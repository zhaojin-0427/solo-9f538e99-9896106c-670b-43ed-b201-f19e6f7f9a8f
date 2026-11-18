# 空运危险品保管链 API

面向断网后乱序补传场景的保管链（chain of custody）服务。数据员可以据此判断：
在某个时刻之前，哪些交接仍然可信、断链发生在哪里、当前可确认的保管方是谁。

技术栈：**FastAPI + SQLite（WAL 模式）+ Uvicorn**，可通过 `docker compose` 一键启动。

## 核心规则

1. **固定起始保管方**：创建运单时登记 `origin_party`，初始可确认保管方即该方。
2. **批量提交**：每笔扫描携带 `scan_id`、正整数 `revision`、UTC 时刻 `ts_utc`、
   交出方 `giver`、接收方 `receiver`。时刻必须带时区偏移（如 `...Z`、`+08:00`），统一归一化为 UTC 存储。
3. **撤回**：只提交 `scan_id` 与更高的 `revision`，`event_type=WITHDRAWAL`，
   不得携带时刻或当事方；撤回只能针对已存在的扫描 ID。
4. **修订裁决**：
   - 同一 `scan_id` 仅采用**最高修订号**的版本；更低修订号提交返回 `409 stale`。
   - 同修订号、异文（时刻/交出方/接收方/类型不同）**整批拒绝**（`409`），已提交数据原样保留。
   - 同修订号、同文重复提交幂等，不计入写入数。
5. **链核对**（按 UTC 时刻升序，乱序补传自动归位）：
   - 交接的 `giver` 必须等于当前保管方（首笔对照起始保管方，其后对照上一笔的 `receiver`），
     不匹配即为 **GAP 断链**。
   - **同一时刻存在多笔交接时不猜测顺序**：该时刻整组标记为 **SIMULTANEOUS** 断链，组内全部未证实。
   - 断链之后的所有扫描一律 `verified=false`，直到补报、更正或撤回补齐缺口；
     重算后断链位置可以前移、后移或消失。
6. **原子性与一致性**：
   - 每个批次在单个 `BEGIN IMMEDIATE` 事务内完成，任何冲突整批回滚，**失败保留旧链**。
   - 查询只读已提交快照（WAL），交错的扫描提交与查询永远看不到半写状态或混合修订。
   - 数据持久化在 SQLite 文件中，进程重启后状态不变。
7. **暴露报告**（调查用）：
   - 对指定 UTC 时刻与指定保管方，逐笔运单给出 `CONFIRMED_AT_PARTY`（确认在该方）、
     `CONFIRMED_NOT_AT_PARTY`（确认不在该方）或 `UNDETERMINABLE`（无法判定）。
   - 判定只沿**已证实**交接推进：交接时刻归**接收方**（`ts <= at_utc`），
     早于首笔交接按**起始方**，**断链时刻起不得推定保管方**（返回断链位置）。
   - 每笔结论附判定依据：依据的 `scan_id` + `revision`，或 `ORIGIN`，
     或 `BREAK` 的 `break_scan_ids` / `break_reason` / `break_detail`。
   - 生成时用**一个只读快照**同时固定运单集合与扫描修订，补报、更正、撤回与生成
     交错发生也不会把新旧数据混进同一份报告。
   - 报告的结论、依据与运单集合随报告一起原子提交并持久化：失败不留半份报告，
     重启后仍可查；**旧报告永远保留当时的结论**。
   - 对比接口按报告号给出与**当前已提交状态**相比分类变化的运单及新结论，
     报告之后新增的运单也计入（`NEW_WAYBILL`）。

## 启动方式

### Docker Compose（推荐）

```bash
docker compose up --build
# 服务： http://localhost:8000
# 文档： http://localhost:8000/docs
# 控制台：http://localhost:8000/
```

数据库保存在命名卷 `chain-data`（容器内 `/data/chain.db`），容器重启不丢数据。

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 可选：APP_DB_PATH=/custom/path/chain.db
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/waybills` | 创建运单并固定起始保管方 |
| GET | `/api/waybills` | 列出全部运单 |
| POST | `/api/waybills/{waybill_id}/scans/batch` | 批量提交交接 / 更正 / 撤回 |
| GET | `/api/waybills/{waybill_id}/chain` | 查询可信前缀、断链原因、当前保管方 |
| POST | `/api/exposure-reports` | 生成暴露报告（指定时刻 + 保管方） |
| GET | `/api/exposure-reports` | 列出已生成报告 |
| GET | `/api/exposure-reports/{report_id}` | 按报告号查看当时的结论 |
| GET | `/api/exposure-reports/{report_id}/changes` | 与当前状态对比，返回分类变化与新结论 |
| GET | `/docs` | Swagger UI，可直接录入与查询 |

### 交接批次示例

```json
{
  "scans": [
    {"scan_id": "S1", "revision": 1, "ts_utc": "2026-11-18T08:00:00Z",
     "giver": "HKG-WAREHOUSE", "receiver": "CX-CARGO"},
    {"scan_id": "S2", "revision": 1, "ts_utc": "2026-11-18T10:30:00Z",
     "giver": "CX-CARGO", "receiver": "PVG-WAREHOUSE"}
  ]
}
```

### 撤回示例（修订号必须更高）

```json
{"scans": [{"scan_id": "S2", "revision": 2, "event_type": "WITHDRAWAL"}]}
```

### 查询响应示例（发生断链时）

```json
{
  "waybill_id": "AWB-001",
  "origin_party": "HKG-WAREHOUSE",
  "current_custodian": "CX-CARGO",
  "verified_prefix": [ {"scan_id": "S1", "verified": true, "...": "..."} ],
  "unverified_scans": [ {"scan_id": "S3", "verified": false,
                         "unverified_reason": "GAP"} ],
  "chain_intact": false,
  "break_reason": "GAP",
  "break_detail": "giver 'X' does not hold custody; expected 'CX-CARGO' ...",
  "break_scan_ids": ["S3"]
}
```

`break_reason` 取值：`NONE` / `GAP`（保管方不承接）/ `SIMULTANEOUS`（同时刻多笔，顺序不可判定）。
`current_custodian` 只沿已证实交接推进，因此断链后仍可信地停留在最后一个已证实接收方。

### 暴露报告示例

生成（`party` = 被核查的保管方，`at_utc` = 核查时刻，必须带时区）：

```json
POST /api/exposure-reports
{"party": "CX-CARGO", "at_utc": "2026-11-18T10:30:00Z"}
```

响应（201）：

```json
{
  "report_id": 1,
  "party": "CX-CARGO",
  "at_utc": "2026-11-18T10:30:00Z",
  "created_at": "2026-11-18T22:00:00.123456Z",
  "summary": {"total": 2, "confirmed_at_party": 1,
              "confirmed_not_at_party": 0, "undeterminable": 1},
  "waybills": [
    {"waybill_id": "AWB-001", "origin_party": "HKG-WAREHOUSE",
     "classification": "CONFIRMED_AT_PARTY", "custodian": "CX-CARGO",
     "basis": {"kind": "HANDOVER", "scan_id": "S1", "revision": 1,
               "ts_utc": "2026-11-18T08:00:00Z", "custodian": "CX-CARGO",
               "origin_party": "HKG-WAREHOUSE", "break_scan_ids": []},
     "basis_scan_id": "S1", "basis_revision": 1, "break_scan_ids": []},
    {"waybill_id": "AWB-002", "origin_party": "HKG-WAREHOUSE",
     "classification": "UNDETERMINABLE", "custodian": null,
     "basis": {"kind": "BREAK", "origin_party": "HKG-WAREHOUSE",
               "break_reason": "GAP", "break_scan_ids": ["S9"],
               "break_detail": "giver 'X' does not hold custody; expected 'CX-CARGO' ..."},
     "basis_scan_id": null, "basis_revision": null, "break_scan_ids": ["S9"]}
  ],
  "changes": []
}
```

对比当前状态：

```json
GET /api/exposure-reports/1/changes
{
  "report_id": 1, "party": "CX-CARGO", "at_utc": "2026-11-18T10:30:00Z",
  "changes": [
    {"waybill_id": "AWB-001", "change_type": "CLASSIFICATION_CHANGED",
     "is_new_waybill": false,
     "previous_classification": "CONFIRMED_AT_PARTY",
     "current_classification": "CONFIRMED_NOT_AT_PARTY",
     "new_classification": "CONFIRMED_NOT_AT_PARTY",
     "previous_basis": {"kind": "HANDOVER", "scan_id": "S1", "revision": 1, "...": "..."},
     "current_basis": {"kind": "HANDOVER", "scan_id": "S1", "revision": 2, "...": "..."}},
    {"waybill_id": "AWB-003", "change_type": "NEW_WAYBILL",
     "is_new_waybill": true, "previous_classification": null,
     "current_classification": "CONFIRMED_AT_PARTY",
     "new_classification": "CONFIRMED_AT_PARTY",
     "previous_basis": null, "current_basis": {"...": "..."}}
  ]
}
```

`GET /api/exposure-reports/{id}` 返回的报告正文与生成时一致（旧结论不变），
其 `changes` 字段同样是相对当前状态的实时对比结果。

## 测试

```bash
pip install pytest httpx
pytest -q
```

共 43 个用例，覆盖：乱序补传归位、时区归一化、最高修订裁决、同修订异文整批回滚、
撤回与补报修复断链、GAP/SIMULTANEOUS 检测、输入校验、真实线程下的写串行化、
交错读不到混合修订、进程重启持久化；以及暴露报告的三种结论与判定依据、
交接时刻归接收方、早于首笔交接按起始方、断链起不可推定、生成期间的快照隔离、
失败不留半份报告、旧报告结论不变、按报告号对比变化（含新增运单）、并发生成与重启后查询。
