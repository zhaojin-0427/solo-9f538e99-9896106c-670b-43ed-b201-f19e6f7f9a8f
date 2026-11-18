"""End-to-end HTTP smoke test against a running uvicorn instance."""
import json
import os
import sys
import urllib.request

BASE = "http://127.0.0.1:" + os.environ.get("SMOKE_PORT", "8011")


def call(method, path, body=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = resp.read()
            return resp.status, payload if raw else json.loads(payload)
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload)
        except json.JSONDecodeError:
            return e.code, payload


def check(label, cond, extra=""):
    print(("PASS" if cond else "FAIL"), label, extra)
    if not cond:
        sys.exit(1)


st, body = call("POST", "/api/waybills", {"waybill_id": "AWB-SMOKE", "origin_party": "ORIG"})
check("create waybill", st == 201, body)

st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S1", "revision": 1, "ts_utc": "2026-11-18T08:00:00Z", "giver": "ORIG", "receiver": "B"},
    {"scan_id": "S2", "revision": 1, "ts_utc": "2026-11-18T09:00:00Z", "giver": "B", "receiver": "C"},
    {"scan_id": "S3", "revision": 1, "ts_utc": "2026-11-18T10:00:00Z", "giver": "X", "receiver": "Y"},
]})
check("batch accepted", st == 200, st)
check("GAP detected", body["chain"]["break_reason"] == "GAP")
check("custodian stays at C", body["chain"]["current_custodian"] == "C")
check("S3 unverified", [s["scan_id"] for s in body["chain"]["unverified_scans"]] == ["S3"])

# same revision divergence -> 409, old chain preserved
st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S3", "revision": 1, "ts_utc": "2026-11-18T10:00:00Z", "giver": "X", "receiver": "TAMPERED"},
]})
check("same-revision divergence -> 409", st == 409, body)

st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S3", "revision": 2, "event_type": "WITHDRAWAL"},
]})
check("withdrawal accepted", st == 200, body)
st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S3", "revision": 3, "ts_utc": "2026-11-18T10:00:00Z", "giver": "C", "receiver": "D"},
]})
check("correction accepted", st == 200, st)
check("chain healed", body["chain"]["chain_intact"] is True)
check("custodian now D", body["chain"]["current_custodian"] == "D")

# simultaneous timestamps
st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S4", "revision": 1, "ts_utc": "2026-11-18T11:00:00Z", "giver": "D", "receiver": "E"},
    {"scan_id": "S5", "revision": 1, "ts_utc": "2026-11-18T11:00:00Z", "giver": "E", "receiver": "F"},
]})
check("simultaneous flagged", body["chain"]["break_reason"] == "SIMULTANEOUS")

st, body = call("GET", "/api/waybills/AWB-SMOKE/chain")
check("GET chain", st == 200 and body["waybill_id"] == "AWB-SMOKE")

st, body = call("GET", "/api/waybills")
check("list waybills", st == 200 and any(w["id"] == "AWB-SMOKE" for w in body["waybills"]))

st, body = call("GET", "/docs", raw=True)
check("swagger docs reachable", st == 200 and b"swagger" in body.lower())
st, body = call("GET", "/openapi.json")
check("openapi schema reachable", st == 200 and "paths" in body)
st, body = call("GET", "/", raw=True)
check("console page reachable", st == 200 and b"<!DOCTYPE html>" in body)

# validation: naive datetime
st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S9", "revision": 1, "ts_utc": "2026-11-18T12:00:00", "giver": "D", "receiver": "E"},
]})
check("naive timestamp -> 422", st == 422)

# exposure reports (use a dedicated waybill so the smoke run is repeatable)
st, body = call("POST", "/api/waybills", {"waybill_id": "AWB-EXPOSURE", "origin_party": "ORIG"})
check("create exposure waybill", st == 201, body)
st, body = call("POST", "/api/waybills/AWB-EXPOSURE/scans/batch", {"scans": [
    {"scan_id": "X1", "revision": 1, "ts_utc": "2026-11-18T08:00:00Z", "giver": "ORIG", "receiver": "B"},
    {"scan_id": "X2", "revision": 1, "ts_utc": "2026-11-18T10:00:00Z", "giver": "B", "receiver": "C"},
]})
check("exposure chain accepted", st == 200, body)

st, report = call("POST", "/api/exposure-reports", {"party": "B", "at_utc": "2026-11-18T09:00:00Z"})
check("report created", st == 201, report)
row = next(w for w in report["waybills"] if w["waybill_id"] == "AWB-EXPOSURE")
check("report confirms party", row["classification"] == "CONFIRMED_AT_PARTY", row)
check("report basis carries scan id/revision",
      row["basis_scan_id"] == "X1" and row["basis_revision"] == 1, row["basis"])
check("report has no changes on creation", report["changes"] == [])

# origin rules: before the first handover the origin party holds custody
st, early = call("POST", "/api/exposure-reports", {"party": "ORIG", "at_utc": "2026-11-18T07:00:00Z"})
early_row = next(w for w in early["waybills"] if w["waybill_id"] == "AWB-EXPOSURE")
check("before first handover -> origin", early_row["basis"]["kind"] == "ORIGIN"
      and early_row["classification"] == "CONFIRMED_AT_PARTY", early_row)

# correct the scans after the report; the stored report must keep its conclusion
st, body = call("POST", "/api/waybills/AWB-EXPOSURE/scans/batch", {"scans": [
    {"scan_id": "X2", "revision": 2, "ts_utc": "2026-11-18T09:00:00Z", "giver": "B", "receiver": "D"},
]})
check("correction accepted", st == 200, body)

st, stored = call("GET", f"/api/exposure-reports/{report['report_id']}")
stored_row = next(w for w in stored["waybills"] if w["waybill_id"] == "AWB-EXPOSURE")
check("old report keeps its conclusion", stored_row["classification"] == "CONFIRMED_AT_PARTY", stored_row)
check("old report keeps its basis revision", stored_row["basis_revision"] == 1)

st, changes = call("GET", f"/api/exposure-reports/{report['report_id']}/changes")
changed = {c["waybill_id"]: c for c in changes["changes"]}
check("classification change reported", "AWB-EXPOSURE" in changed, changes)
check("new conclusion reported",
      changed["AWB-EXPOSURE"]["new_classification"] == "CONFIRMED_NOT_AT_PARTY", changed)

# a break at/after its timestamp makes custody undeterminable
st, body = call("POST", "/api/waybills/AWB-EXPOSURE/scans/batch", {"scans": [
    {"scan_id": "X3", "revision": 1, "ts_utc": "2026-11-18T11:00:00Z", "giver": "Z", "receiver": "Y"},
]})
check("gap introduced", body["chain"]["break_reason"] == "GAP", body["chain"])
st, broken = call("POST", "/api/exposure-reports", {"party": "Y", "at_utc": "2026-11-18T11:30:00Z"})
broken_row = next(w for w in broken["waybills"] if w["waybill_id"] == "AWB-EXPOSURE")
check("break -> undeterminable", broken_row["classification"] == "UNDETERMINABLE", broken_row)
check("break location reported", broken_row["break_scan_ids"] == ["X3"], broken_row)

st, body = call("GET", "/api/exposure-reports")
check("report listing", st == 200 and any(
    r["report_id"] == report["report_id"] for r in body["reports"]), body)

print("ALL SMOKE CHECKS PASSED")
