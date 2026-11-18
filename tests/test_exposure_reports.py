"""Exposure reports: classification, basis, snapshot isolation, persistence."""
from __future__ import annotations

import threading

from conftest import handover, make_waybill, withdrawal


def report(client, party, at_utc):
    r = client.post(
        "/api/exposure-reports", json={"party": party, "at_utc": at_utc}
    )
    assert r.status_code == 201, r.text
    return r.json()


def by_id(report_body, waybill_id):
    matches = [w for w in report_body["waybills"] if w["waybill_id"] == waybill_id]
    assert len(matches) == 1, report_body
    return matches[0]


# --------------------------------------------------------------------------- #
# Classification rules
# --------------------------------------------------------------------------- #
def test_before_first_handover_origin_holds_custody(client):
    make_waybill(client, origin="ORIG")
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")]},
    )
    body = report(client, "ORIG", "2026-11-18T07:59:59Z")
    row = by_id(body, "AWB-1")
    assert row["classification"] == "CONFIRMED_AT_PARTY"
    assert row["custodian"] == "ORIG"
    assert row["basis"]["kind"] == "ORIGIN"
    assert row["basis_scan_id"] is None

    # origin is confirmed NOT at another party
    body = report(client, "B", "2026-11-18T07:59:59Z")
    assert by_id(body, "AWB-1")["classification"] == "CONFIRMED_NOT_AT_PARTY"


def test_handover_instant_belongs_to_receiver(client):
    """At exactly ts the receiver holds; one microsecond earlier the giver."""
    make_waybill(client, origin="ORIG")
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={
            "scans": [
                handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
                handover("S2", 1, "2026-11-18T10:00:00Z", "B", "C"),
            ]
        },
    )
    at = by_id(report(client, "C", "2026-11-18T10:00:00Z"), "AWB-1")
    assert at["classification"] == "CONFIRMED_AT_PARTY"
    assert at["basis"]["scan_id"] == "S2"
    assert at["basis"]["revision"] == 1

    # the giver has already handed over at the same instant
    giver_at = by_id(report(client, "B", "2026-11-18T10:00:00Z"), "AWB-1")
    assert giver_at["classification"] == "CONFIRMED_NOT_AT_PARTY"

    before = by_id(report(client, "B", "2026-11-18T09:59:59.999999Z"), "AWB-1")
    assert before["classification"] == "CONFIRMED_AT_PARTY"
    assert before["basis"]["scan_id"] == "S1"

    previous_holder = by_id(report(client, "ORIG", "2026-11-18T09:59:59Z"), "AWB-1")
    assert previous_holder["classification"] == "CONFIRMED_NOT_AT_PARTY"


def test_timezone_offsets_are_normalized(client):
    make_waybill(client, origin="ORIG")
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S1", 1, "2026-11-18T16:00:00+08:00", "ORIG", "B")]},
    )
    # 16:00+08:00 == 08:00Z, so the handover instant belongs to B
    body = report(client, "B", "2026-11-18T16:00:00+08:00")
    assert body["at_utc"].startswith("2026-11-18T08:00:00")
    row = by_id(body, "AWB-1")
    assert row["classification"] == "CONFIRMED_AT_PARTY"
    assert row["basis"]["scan_id"] == "S1"


def test_break_makes_custody_undeterminable_with_location(client):
    make_waybill(client, origin="ORIG")
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={
            "scans": [
                handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
                handover("S2", 1, "2026-11-18T10:00:00Z", "X", "C"),  # GAP
                handover("S3", 1, "2026-11-18T11:00:00Z", "C", "D"),
            ]
        },
    )
    # before the break instant custody is still determinable
    before = by_id(report(client, "B", "2026-11-18T09:59:59Z"), "AWB-1")
    assert before["classification"] == "CONFIRMED_AT_PARTY"
    assert before["basis"]["scan_id"] == "S1"

    # from the break instant onwards no custody may be presumed
    at_break = by_id(report(client, "B", "2026-11-18T10:00:00Z"), "AWB-1")
    assert at_break["classification"] == "UNDETERMINABLE"
    assert at_break["custodian"] is None
    assert at_break["basis"]["kind"] == "BREAK"
    assert at_break["basis"]["break_reason"] == "GAP"
    assert at_break["basis"]["break_scan_ids"] == ["S2"]
    assert at_break["break_scan_ids"] == ["S2"]

    # even a party that appears as receiver of a later (unverified) scan is
    # not presumed to hold custody
    later = by_id(report(client, "D", "2026-11-18T11:00:00Z"), "AWB-1")
    assert later["classification"] == "UNDETERMINABLE"


def test_simultaneous_break_is_undeterminable(client):
    make_waybill(client, origin="ORIG")
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={
            "scans": [
                handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
                handover("S2", 1, "2026-11-18T09:00:00Z", "B", "C"),
                handover("S3", 1, "2026-11-18T09:00:00Z", "C", "D"),
            ]
        },
    )
    row = by_id(report(client, "C", "2026-11-18T09:00:00Z"), "AWB-1")
    assert row["classification"] == "UNDETERMINABLE"
    assert row["basis"]["break_reason"] == "SIMULTANEOUS"
    assert sorted(row["basis"]["break_scan_ids"]) == ["S2", "S3"]


def test_correction_and_backfill_can_change_current_conclusion(client):
    make_waybill(client, origin="ORIG")
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S2", 1, "2026-11-18T10:00:00Z", "B", "C")]},
    )
    first = report(client, "C", "2026-11-18T10:00:00Z")
    assert by_id(first, "AWB-1")["classification"] == "UNDETERMINABLE"

    # backfill S1 -> chain heals, C is now confirmed at the instant
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")]},
    )
    second = report(client, "C", "2026-11-18T10:00:00Z")
    row = by_id(second, "AWB-1")
    assert row["classification"] == "CONFIRMED_AT_PARTY"
    assert row["basis"]["scan_id"] == "S2"


def test_every_registered_waybill_is_reported(client):
    make_waybill(client, wid="AWB-1", origin="ORIG")
    make_waybill(client, wid="AWB-2", origin="ORIG")
    body = report(client, "ORIG", "2026-11-18T08:00:00Z")
    assert [w["waybill_id"] for w in body["waybills"]] == ["AWB-1", "AWB-2"]
    assert body["summary"] == {
        "total": 2,
        "confirmed_at_party": 2,
        "confirmed_not_at_party": 0,
        "undeterminable": 0,
    }


# --------------------------------------------------------------------------- #
# Stored reports keep their conclusions; changes are compared to now
# --------------------------------------------------------------------------- #
def test_stored_report_is_immutable_and_changes_are_reported(client):
    make_waybill(client, origin="ORIG")
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")]},
    )
    first = report(client, "B", "2026-11-18T08:00:00Z")
    assert by_id(first, "AWB-1")["classification"] == "CONFIRMED_AT_PARTY"
    assert first["changes"] == []

    # correction: same timestamp, different receiver (revision 2)
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S1", 2, "2026-11-18T08:00:00Z", "ORIG", "C")]},
    )

    stored = client.get(f"/api/exposure-reports/{first['report_id']}")
    assert stored.status_code == 200
    stored_body = stored.json()
    # conclusions of the moment are preserved verbatim
    assert by_id(stored_body, "AWB-1")["classification"] == "CONFIRMED_AT_PARTY"
    assert by_id(stored_body, "AWB-1")["basis"]["revision"] == 1
    assert stored_body["summary"]["confirmed_at_party"] == 1

    changes = client.get(
        f"/api/exposure-reports/{first['report_id']}/changes"
    ).json()["changes"]
    assert len(changes) == 1
    ch = changes[0]
    assert ch["waybill_id"] == "AWB-1"
    assert ch["change_type"] == "CLASSIFICATION_CHANGED"
    assert ch["previous_classification"] == "CONFIRMED_AT_PARTY"
    assert ch["current_classification"] == "CONFIRMED_NOT_AT_PARTY"
    assert ch["new_classification"] == "CONFIRMED_NOT_AT_PARTY"
    assert ch["previous_basis"]["revision"] == 1
    assert ch["current_basis"]["revision"] == 2


def test_withdrawal_can_flip_classification_to_undeterminable(client):
    make_waybill(client, origin="ORIG")
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={
            "scans": [
                handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
                handover("S2", 1, "2026-11-18T10:00:00Z", "B", "C"),
            ]
        },
    )
    first = report(client, "C", "2026-11-18T10:00:00Z")
    assert by_id(first, "AWB-1")["classification"] == "CONFIRMED_AT_PARTY"

    client.post(
        "/api/waybills/AWB-1/scans/batch", json={"scans": [withdrawal("S2", 2)]}
    )
    ch = client.get(f"/api/exposure-reports/{first['report_id']}/changes").json()[
        "changes"
    ][0]
    # S2 withdrawn: at 10:00 the last verified handover is S1 -> B
    assert ch["current_classification"] == "CONFIRMED_NOT_AT_PARTY"

    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S1", 2, "2026-11-18T10:00:00Z", "ORIG", "B")]},
    )
    # S1 moved to 10:00 still keeps the chain intact and C is not at party
    ch = client.get(f"/api/exposure-reports/{first['report_id']}/changes").json()[
        "changes"
    ][0]
    assert ch["current_classification"] == "CONFIRMED_NOT_AT_PARTY"


def test_new_waybill_after_report_counts_as_change(client):
    make_waybill(client, wid="AWB-1", origin="ORIG")
    first = report(client, "ORIG", "2026-11-18T08:00:00Z")
    assert [c["change_type"] for c in first["changes"]] == []

    make_waybill(client, wid="AWB-2", origin="ORIG")
    make_waybill(client, wid="AWB-3", origin="OTHER")
    changes = client.get(
        f"/api/exposure-reports/{first['report_id']}/changes"
    ).json()["changes"]
    assert [(c["waybill_id"], c["change_type"], c["is_new_waybill"]) for c in changes] == [
        ("AWB-2", "NEW_WAYBILL", True),
        ("AWB-3", "NEW_WAYBILL", True),
    ]
    assert changes[0]["new_classification"] == "CONFIRMED_AT_PARTY"
    assert changes[1]["new_classification"] == "CONFIRMED_NOT_AT_PARTY"
    assert changes[0]["previous_classification"] is None


def test_report_listing_and_unknown_report(client):
    make_waybill(client, origin="ORIG")
    r1 = report(client, "ORIG", "2026-11-18T08:00:00Z")
    r2 = report(client, "ORIG", "2026-11-18T09:00:00Z")
    listing = client.get("/api/exposure-reports").json()["reports"]
    assert [r["report_id"] for r in listing] == [r1["report_id"], r2["report_id"]]
    assert listing[0]["summary"]["total"] == 1

    assert client.get("/api/exposure-reports/9999").status_code == 404
    assert client.get("/api/exposure-reports/9999/changes").status_code == 404


def test_naive_instant_and_missing_party_rejected(client):
    assert (
        client.post(
            "/api/exposure-reports",
            json={"party": "ORIG", "at_utc": "2026-11-18T08:00:00"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/exposure-reports",
            json={"at_utc": "2026-11-18T08:00:00Z"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/exposure-reports",
            json={"party": "  ", "at_utc": "2026-11-18T08:00:00Z"},
        ).status_code
        == 422
    )


# --------------------------------------------------------------------------- #
# Snapshot isolation, atomicity, persistence
# --------------------------------------------------------------------------- #
def test_report_uses_one_snapshot_despite_interleaved_correction(client, monkeypatch):
    """A correction committed mid-generation must not leak into the report."""
    from app import config, database, schemas, services

    make_waybill(client, wid="AWB-1", origin="ORIG")
    make_waybill(client, wid="AWB-2", origin="ORIG")
    for wid in ("AWB-1", "AWB-2"):
        client.post(
            f"/api/waybills/{wid}/scans/batch",
            json={"scans": [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")]},
        )

    db_path = config.DB_PATH
    real_eval = services._evaluate_chain
    calls = {"n": 0}

    def eval_with_interleaved_write(conn, waybill_id):
        calls["n"] += 1
        if calls["n"] == 1:
            # A separate connection commits a correction while the report's
            # read snapshot is already open.
            writer = database.connect(db_path)
            try:
                services.apply_batch(
                    writer,
                    "AWB-2",
                    schemas.BatchSubmit(
                        scans=[
                            schemas.ScanEntry(
                                scan_id="S1",
                                revision=2,
                                ts_utc=__import__("datetime").datetime(
                                    2026, 11, 18, 8, tzinfo=__import__("datetime").timezone.utc
                                ),
                                giver="ORIG",
                                receiver="C",
                            )
                        ]
                    ),
                )
            finally:
                writer.close()
        return real_eval(conn, waybill_id)

    monkeypatch.setattr(services, "_evaluate_chain", eval_with_interleaved_write)

    body = report(client, "B", "2026-11-18T08:00:00Z")
    monkeypatch.setattr(services, "_evaluate_chain", real_eval)

    # the report saw the pre-correction snapshot for BOTH waybills
    assert by_id(body, "AWB-1")["classification"] == "CONFIRMED_AT_PARTY"
    assert by_id(body, "AWB-2")["classification"] == "CONFIRMED_AT_PARTY"
    assert by_id(body, "AWB-2")["basis"]["revision"] == 1

    # the correction did commit and is visible in the current state
    chain = client.get("/api/waybills/AWB-2/chain").json()
    assert chain["verified_prefix"][0]["revision"] == 2
    assert chain["current_custodian"] == "C"
    changes = client.get(
        f"/api/exposure-reports/{body['report_id']}/changes"
    ).json()["changes"]
    assert [c["waybill_id"] for c in changes] == ["AWB-2"]


def test_failed_generation_leaves_no_partial_report(client, monkeypatch):
    from app.schemas import ExposureReportResponse

    make_waybill(client, origin="ORIG")
    before = client.get("/api/exposure-reports").json()["reports"]

    def boom(self):
        raise RuntimeError("serialization failed")

    monkeypatch.setattr(ExposureReportResponse, "model_dump_json", boom)
    from fastapi.testclient import TestClient

    with TestClient(client.app, raise_server_exceptions=False) as raw:
        r = raw.post(
            "/api/exposure-reports",
            json={"party": "ORIG", "at_utc": "2026-11-18T08:00:00Z"},
        )
    assert r.status_code == 500
    monkeypatch.undo()

    # no half-written report survives (not even a header row)
    after = client.get("/api/exposure-reports").json()["reports"]
    assert after == before

    # and the service is still usable afterwards
    ok = report(client, "ORIG", "2026-11-18T08:00:00Z")
    assert ok["report_id"] == 1


def test_reports_survive_restart(client):
    make_waybill(client, origin="ORIG")
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")]},
    )
    created = report(client, "B", "2026-11-18T08:00:00Z")

    import importlib

    from app import config, main
    from fastapi.testclient import TestClient

    db_path = config.DB_PATH
    importlib.reload(main)
    with TestClient(main.app) as fresh:
        got = fresh.get(f"/api/exposure-reports/{created['report_id']}")
        assert got.status_code == 200
        assert by_id(got.json(), "AWB-1")["classification"] == "CONFIRMED_AT_PARTY"
        assert by_id(got.json(), "AWB-1")["basis"]["scan_id"] == "S1"
        assert fresh.get("/api/waybills/AWB-1/chain").json()["current_custodian"] == "B"
    assert db_path.exists()


def test_concurrent_report_generation_is_consistent(client):
    """Many concurrent generators: every report is internally consistent."""
    from app import config, database, schemas, services

    make_waybill(client, wid="AWB-1", origin="ORIG")
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={
            "scans": [
                handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
                handover("S2", 1, "2026-11-18T09:00:00Z", "B", "C"),
            ]
        },
    )
    db_path = config.DB_PATH
    reports: list = []
    errors: list = []
    lock = threading.Lock()

    def worker() -> None:
        conn = database.connect(db_path)
        try:
            body = services.generate_exposure_report(
                conn,
                schemas.ExposureReportCreate(
                    party="B", at_utc=__import__("datetime").datetime(
                        2026, 11, 18, 8, tzinfo=__import__("datetime").timezone.utc
                    )
                ),
            )
            row = body.waybills[0]
            with lock:
                reports.append((body.report_id, row.classification, row.basis.scan_id))
        except Exception as exc:  # pragma: no cover
            with lock:
                errors.append(repr(exc))
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert len(reports) == 8
    assert len({rid for rid, _, _ in reports}) == 8
    assert all(cls.value == "CONFIRMED_AT_PARTY" and sid == "S1"
               for _, cls, sid in reports)
