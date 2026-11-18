"""FastAPI application: dangerous-goods custody chain API."""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError

from . import config, services
from .database import connect, init_db
from .schemas import (
    BatchResponse,
    BatchSubmit,
    ChainResponse,
    ExposureChangesResponse,
    ExposureReportCreate,
    ExposureReportList,
    ExposureReportResponse,
    WaybillCreate,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> None:
    init_db()
    yield


app = FastAPI(
    title="空运危险品保管链 API",
    description=(
        "运单保管链（扫描交接 / 撤回 / 补报 / 更正）的修订裁决与可信前缀查询。"
        "同 ID 仅最高修订生效；同修订异文整批拒绝；失败保留旧链。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


def get_conn() -> sqlite3.Connection:  # type: ignore[return]
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Error mapping: domain errors never leak 500s, and failed batches have
# already been rolled back by the service layer.
# --------------------------------------------------------------------------- #
@app.exception_handler(services.DomainConflict)
async def conflict_handler(_: Request, exc: services.DomainConflict) -> JSONResponse:
    return JSONResponse(status_code=409, content={"error": "conflict", "detail": str(exc)})


@app.exception_handler(services.DomainValidation)
async def not_found_handler(_: Request, exc: services.DomainValidation) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": "not_found", "detail": str(exc)})


@app.exception_handler(ValidationError)
async def pydantic_handler(_: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": "validation_error", "detail": exc.errors()})


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.post("/api/waybills", status_code=201, tags=["waybills"])
def create_waybill(
    body: WaybillCreate, conn: sqlite3.Connection = Depends(get_conn)
) -> dict:
    """创建运单并固定其起始保管方。"""
    return services.create_waybill(conn, body)


@app.get("/api/waybills", tags=["waybills"])
def list_waybills(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    rows = conn.execute(
        "SELECT id, origin_party, created_at FROM waybills ORDER BY id"
    ).fetchall()
    return {"waybills": [dict(r) for r in rows]}


@app.post(
    "/api/waybills/{waybill_id}/scans/batch",
    response_model=BatchResponse,
    tags=["scans"],
)
def submit_batch(
    waybill_id: str,
    batch: BatchSubmit,
    conn: sqlite3.Connection = Depends(get_conn),
) -> BatchResponse:
    """批量提交交接/撤回。

    * HANDOVER：scan_id、revision、ts_utc（带时区）、giver、receiver
    * WITHDRAWAL：仅 scan_id、revision（必须高于已知最高修订）
    * 同修订异文、过期修订、撤回未知 ID -> 409，整批回滚，旧链保留
    """
    return services.apply_batch(conn, waybill_id, batch)


@app.get(
    "/api/waybills/{waybill_id}/chain",
    response_model=ChainResponse,
    tags=["scans"],
)
def query_chain(
    waybill_id: str, conn: sqlite3.Connection = Depends(get_conn)
) -> ChainResponse:
    """返回可信连续交接前缀、首处断链原因与当前可确认保管方。"""
    return services.compute_chain(conn, waybill_id)


# --------------------------------------------------------------------------- #
# Exposure reports
# --------------------------------------------------------------------------- #
@app.post(
    "/api/exposure-reports",
    response_model=ExposureReportResponse,
    status_code=201,
    tags=["exposure-reports"],
)
def create_exposure_report(
    body: ExposureReportCreate, conn: sqlite3.Connection = Depends(get_conn)
) -> ExposureReportResponse:
    """生成暴露报告：在指定 UTC 时刻核查某保管方是否持有各已登记运单。

    结论取值：`CONFIRMED_AT_PARTY`（确认在该方）/ `CONFIRMED_NOT_AT_PARTY`
    （确认不在该方）/ `UNDETERMINABLE`（无法判定）。每笔运单附判定依据：

    * `HANDOVER`：依据的已证实交接 `scan_id` 与 `revision`（交接时刻归接收方）；
    * `ORIGIN`：早于首笔交接，按运单固定起始保管方；
    * `BREAK`：断链时刻起不得推定保管方，附 `break_scan_ids` / `break_reason`。

    报告固定同一已提交状态的运单集合与扫描，补报/更正/撤回交错发生也不会
    混用新旧数据；报告一次性提交，失败不留半份报告，重启后仍可查询。
    """
    return services.generate_exposure_report(conn, body)


@app.get(
    "/api/exposure-reports",
    response_model=ExposureReportList,
    tags=["exposure-reports"],
)
def list_exposure_reports(
    conn: sqlite3.Connection = Depends(get_conn),
) -> ExposureReportList:
    """列出已生成的报告（旧报告的结论保持当时结果）。"""
    return services.list_exposure_reports(conn)


@app.get(
    "/api/exposure-reports/{report_id}",
    response_model=ExposureReportResponse,
    tags=["exposure-reports"],
)
def get_exposure_report(
    report_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> ExposureReportResponse:
    """按报告号查看当时保存的结论（不随之后的数据变化）。"""
    return services.get_exposure_report(conn, report_id)


@app.get(
    "/api/exposure-reports/{report_id}/changes",
    response_model=ExposureChangesResponse,
    tags=["exposure-reports"],
)
def compare_exposure_report(
    report_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> ExposureChangesResponse:
    """按报告号对比当前状态：返回分类发生变化的运单及其新结论。

    报告之后新增的运单也计入，`change_type=NEW_WAYBILL`。
    """
    return services.compare_exposure_report(conn, report_id)


# --------------------------------------------------------------------------- #
# Minimal console (the interactive /docs remains fully functional too).
# --------------------------------------------------------------------------- #
_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")
