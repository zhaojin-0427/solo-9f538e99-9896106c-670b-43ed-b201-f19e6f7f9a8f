"""Domain logic: revision arbitration, atomic batch apply, chain recomputation.

All write paths run inside one ``BEGIN IMMEDIATE`` transaction.  Any rule
violation raises ``DomainConflict`` / ``DomainValidation`` and rolls the whole
batch back, so the previously committed chain is preserved untouched.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .schemas import (
    BatchResponse,
    BatchSubmit,
    BreakReason,
    ChainResponse,
    ExposureBasis,
    ExposureChange,
    ExposureChangesResponse,
    ExposureClassification,
    ExposureReportCreate,
    ExposureReportListItem,
    ExposureReportList,
    ExposureReportResponse,
    ExposureSummary,
    ExposureWaybillResult,
    ScanEntry,
    ScanView,
    WaybillCreate,
    entry_payload_hash,
    to_utc_iso,
    utcnow_iso,
)

_STORAGE_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


class DomainConflict(Exception):
    """HTTP 409: same-revision divergence, stale revision, unknown scan ..."""


class DomainValidation(Exception):
    """HTTP 404: referenced waybill/resource does not exist."""


# --------------------------------------------------------------------------- #
# Waybills
# --------------------------------------------------------------------------- #
def create_waybill(conn: sqlite3.Connection, body: WaybillCreate) -> dict:
    from .database import transaction

    with transaction(conn):
        existing = conn.execute(
            "SELECT id FROM waybills WHERE id = ?", (body.waybill_id,)
        ).fetchone()
        if existing is not None:
            raise DomainConflict(f"waybill {body.waybill_id!r} already exists")
        conn.execute(
            "INSERT INTO waybills (id, origin_party, created_at) VALUES (?, ?, ?)",
            (body.waybill_id, body.origin_party, utcnow_iso()),
        )
    return {"waybill_id": body.waybill_id, "origin_party": body.origin_party}


def get_waybill(conn: sqlite3.Connection, waybill_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, origin_party FROM waybills WHERE id = ?", (waybill_id,)
    ).fetchone()
    if row is None:
        raise DomainValidation(f"waybill {waybill_id!r} not found")
    return row


# --------------------------------------------------------------------------- #
# Batch submission (handover reports, corrections, withdrawals)
# --------------------------------------------------------------------------- #
def apply_batch(
    conn: sqlite3.Connection, waybill_id: str, batch: BatchSubmit
) -> BatchResponse:
    from .database import transaction

    get_waybill(conn, waybill_id)  # 404-style check before opening the txn

    applied = 0
    idempotent = 0
    withdrawn: list[str] = []

    with transaction(conn):
        for entry in batch.scans:
            wrote = _apply_one(conn, waybill_id, entry)
            if not wrote:
                idempotent += 1
                continue
            if entry.event_type == "WITHDRAWAL":
                withdrawn.append(entry.scan_id)
            applied += 1

    # Recompute against the freshly committed snapshot.  Scan writes and the
    # returned query result therefore become visible together.
    chain = compute_chain(conn, waybill_id)
    return BatchResponse(
        waybill_id=waybill_id,
        applied=applied,
        idempotent_duplicates=idempotent,
        withdrawn_scan_ids=withdrawn,
        chain=chain,
    )


def _apply_one(conn: sqlite3.Connection, waybill_id: str, entry: ScanEntry) -> bool:
    """Insert one scan revision.  Returns False when it was an idempotent no-op."""
    h = entry_payload_hash(entry)

    newest = conn.execute(
        "SELECT revision, event_type, payload_hash "
        "FROM scans WHERE waybill_id = ? AND scan_id = ? "
        "ORDER BY revision DESC LIMIT 1",
        (waybill_id, entry.scan_id),
    ).fetchone()

    if newest is not None and newest["revision"] > entry.revision:
        raise DomainConflict(
            f"scan_id={entry.scan_id!r} revision={entry.revision} is stale: "
            f"revision {newest['revision']} already adopted"
        )

    same_rev = conn.execute(
        "SELECT event_type, payload_hash FROM scans "
        "WHERE waybill_id = ? AND scan_id = ? AND revision = ?",
        (waybill_id, entry.scan_id, entry.revision),
    ).fetchone()

    if same_rev is not None:
        # 同修订异文 -> 整批拒绝；同文 -> 幂等跳过
        if same_rev["payload_hash"] != h:
            raise DomainConflict(
                f"scan_id={entry.scan_id!r} revision={entry.revision} already "
                "exists with different content (same-revision divergence)"
            )
        return False

    if entry.event_type == "WITHDRAWAL" and newest is None:
        raise DomainConflict(
            f"cannot withdraw unknown scan_id={entry.scan_id!r}: no prior revision exists"
        )

    ts_text = to_utc_iso(entry.ts_utc) if entry.ts_utc is not None else None
    conn.execute(
        "INSERT INTO scans (waybill_id, scan_id, revision, event_type, "
        "ts_utc, giver, receiver, payload_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            waybill_id,
            entry.scan_id,
            entry.revision,
            entry.event_type,
            ts_text,
            entry.giver,
            entry.receiver,
            h,
            utcnow_iso(),
        ),
    )
    return True


# --------------------------------------------------------------------------- #
# Chain recomputation
# --------------------------------------------------------------------------- #
def _adopted_handovers(
    conn: sqlite3.Connection, waybill_id: str
) -> list[sqlite3.Row]:
    """Return handover rows for each scan's highest revision, time-ordered.

    Scans whose highest revision is a WITHDRAWAL tombstone do not appear.
    """
    return conn.execute(
        """
        SELECT s.scan_id, s.revision, s.event_type, s.ts_utc, s.giver, s.receiver
        FROM scans AS s
        JOIN (
            SELECT scan_id, MAX(revision) AS max_revision
            FROM scans
            WHERE waybill_id = ?
            GROUP BY scan_id
        ) AS m
          ON m.scan_id = s.scan_id AND m.max_revision = s.revision
        WHERE s.waybill_id = ? AND s.event_type = 'HANDOVER'
        ORDER BY s.ts_utc ASC, s.scan_id ASC
        """,
        (waybill_id, waybill_id),
    ).fetchall()


def _parse_ts(text: str) -> datetime:
    return datetime.strptime(text, _STORAGE_FMT).replace(tzinfo=timezone.utc)


@dataclass
class _ChainEval:
    """Internal, fully evaluated chain used by the public chain and reports."""

    origin: str
    custodian: str
    verified: list[tuple[sqlite3.Row, bool]] = field(default_factory=list)
    unverified: list[tuple[sqlite3.Row, bool]] = field(default_factory=list)
    broken: bool = False
    break_reason: BreakReason = BreakReason.none
    break_detail: str | None = None
    break_scan_ids: list[str] = field(default_factory=list)
    break_ts: str | None = None  # first timestamp at/after which custody is unknown

    @property
    def verified_rows(self) -> list[sqlite3.Row]:
        return [row for row, _ in self.verified]


def _evaluate_chain(
    conn: sqlite3.Connection, waybill_id: str
) -> tuple[sqlite3.Row, _ChainEval]:
    """Evaluate the adopted handovers once, in time order.

    A single pass decides which handovers are verified and where the first
    break is.  Both the public chain endpoint and exposure reports consume
    this evaluation, so their notion of "verified" can never diverge.
    """
    waybill = get_waybill(conn, waybill_id)
    origin = waybill["origin_party"]
    rows = _adopted_handovers(conn, waybill_id)

    # Group by timestamp: simultaneous handovers cannot be ordered by guessing.
    groups: list[tuple[str, list[sqlite3.Row]]] = []
    for row in rows:
        if groups and groups[-1][0] == row["ts_utc"]:
            groups[-1][1].append(row)
        else:
            groups.append((row["ts_utc"], [row]))

    state = _ChainEval(origin=origin, custodian=origin)

    for ts, group in groups:
        if state.broken:
            # 断链之后的扫描一律未证实，直到补报/更正/撤回补齐缺口
            state.unverified.extend((r, False) for r in group)
            continue

        if len(group) > 1:
            state.broken = True
            state.break_reason = BreakReason.simultaneous
            state.break_scan_ids = [r["scan_id"] for r in group]
            state.break_ts = ts
            state.break_detail = (
                f"{len(group)} handovers share ts_utc={ts}; order cannot be "
                "determined, so none of them is verified"
            )
            state.unverified.extend((r, False) for r in group)
            continue

        scan = group[0]
        if scan["giver"] != state.custodian:
            state.broken = True
            state.break_reason = BreakReason.gap
            state.break_scan_ids = [scan["scan_id"]]
            state.break_ts = ts
            state.break_detail = (
                f"giver {scan['giver']!r} does not hold custody; expected "
                f"{state.custodian!r} (previous receiver / origin party)"
            )
            state.unverified.append((scan, False))
            continue

        state.verified.append((scan, True))
        state.custodian = scan["receiver"]

    return waybill, state


def compute_chain(conn: sqlite3.Connection, waybill_id: str) -> ChainResponse:
    waybill, state = _evaluate_chain(conn, waybill_id)

    def view(row: sqlite3.Row, ok: bool) -> ScanView:
        return ScanView(
            scan_id=row["scan_id"],
            revision=row["revision"],
            event_type=row["event_type"],
            ts_utc=_parse_ts(row["ts_utc"]),
            giver=row["giver"],
            receiver=row["receiver"],
            verified=ok,
            unverified_reason=None if ok else state.break_reason,
        )

    return ChainResponse(
        waybill_id=waybill_id,
        origin_party=state.origin,
        current_custodian=state.custodian,
        verified_prefix=[view(r, True) for r, _ in state.verified],
        unverified_scans=[view(r, False) for r, _ in state.unverified],
        chain_intact=not state.broken,
        break_reason=state.break_reason,
        break_detail=state.break_detail,
        break_scan_ids=state.break_scan_ids,
    )


# --------------------------------------------------------------------------- #
# Exposure reports
# --------------------------------------------------------------------------- #
def _custody_at(
    state: _ChainEval, party: str, at_utc: datetime
) -> tuple[ExposureClassification, str | None, ExposureBasis]:
    """Decide one waybill's custody at ``at_utc`` from an evaluated chain.

    Rules:

    * the instant of a handover belongs to the **receiver** (``ts <= at``);
    * before the first handover the fixed **origin party** holds custody;
    * from the first break timestamp onwards custody may **not** be presumed,
      so the conclusion is ``UNDETERMINABLE`` and the break location is given.

    ``at_utc`` is normalized exactly like stored timestamps, so offset
    comparisons are exact down to the microsecond.
    """
    at_key = to_utc_iso(at_utc)

    if state.broken and state.break_ts is not None and at_key >= state.break_ts:
        basis = ExposureBasis(
            kind="BREAK",
            origin_party=state.origin,
            break_reason=state.break_reason,
            break_scan_ids=list(state.break_scan_ids),
            break_detail=state.break_detail,
        )
        return ExposureClassification.undeterminable, None, basis

    # Last verified handover at or before the instant; its receiver holds it.
    last: sqlite3.Row | None = None
    for row, _ok in state.verified:
        if row["ts_utc"] <= at_key:
            last = row
        else:
            break

    if last is None:
        # Earlier than the first handover (or no handover at all).
        basis = ExposureBasis(kind="ORIGIN", origin_party=state.origin,
                              custodian=state.origin)
        cls = (ExposureClassification.at_party if state.origin == party
               else ExposureClassification.not_at_party)
        return cls, state.origin, basis

    custodian = last["receiver"]
    basis = ExposureBasis(
        kind="HANDOVER",
        scan_id=last["scan_id"],
        revision=last["revision"],
        ts_utc=_parse_ts(last["ts_utc"]),
        custodian=custodian,
        origin_party=state.origin,
    )
    cls = (ExposureClassification.at_party if custodian == party
           else ExposureClassification.not_at_party)
    return cls, custodian, basis


def _waybill_ids(conn: sqlite3.Connection) -> list[str]:
    """The fixed waybill set for one snapshot (creation order)."""
    rows = conn.execute(
        "SELECT id FROM waybills ORDER BY rowid"
    ).fetchall()
    return [r["id"] for r in rows]


def _summarize(results: list[ExposureWaybillResult]) -> ExposureSummary:
    return ExposureSummary(
        total=len(results),
        confirmed_at_party=sum(
            r.classification == ExposureClassification.at_party for r in results
        ),
        confirmed_not_at_party=sum(
            r.classification == ExposureClassification.not_at_party for r in results
        ),
        undeterminable=sum(
            r.classification == ExposureClassification.undeterminable for r in results
        ),
    )


def _evaluate_report_from_snapshot(
    conn: sqlite3.Connection, party: str, at_utc: datetime
) -> tuple[list[ExposureWaybillResult], ExposureSummary]:
    """Compute every waybill's conclusion from the current read snapshot.

    Callers must already be inside :func:`database.read_snapshot` or a
    write transaction; all reads below therefore see exactly one committed
    set of waybills and revisions.
    """
    results: list[ExposureWaybillResult] = []
    for wid in _waybill_ids(conn):
        _, state = _evaluate_chain(conn, wid)
        cls, custodian, basis = _custody_at(state, party, at_utc)
        results.append(
            ExposureWaybillResult(
                waybill_id=wid,
                origin_party=state.origin,
                classification=cls,
                custodian=custodian,
                basis=basis,
                basis_scan_id=basis.scan_id,
                basis_revision=basis.revision,
                break_scan_ids=list(basis.break_scan_ids),
            )
        )
    return results, _summarize(results)


def generate_exposure_report(
    conn: sqlite3.Connection, body: ExposureReportCreate
) -> ExposureReportResponse:
    """Generate, persist and return an immutable exposure report.

    The report rows are written in a single transaction: either the whole
    report (header + snapshot payload) is committed or nothing at all.  A
    failure therefore never leaves half a report behind, and once committed
    it is readable after a restart.
    """
    from .database import read_snapshot, transaction

    at_utc = body.at_utc.astimezone(timezone.utc)

    # One read snapshot for the entire report: no backfill/correction/
    # withdrawal committed by another connection can be mixed into it, and
    # the waybill set and scan revisions are pinned together at the first
    # read.  Writers are not blocked while the report is being computed.
    with read_snapshot(conn):
        results, summary = _evaluate_report_from_snapshot(conn, body.party, at_utc)

        response = ExposureReportResponse(
            report_id=0,
            party=body.party,
            at_utc=at_utc,
            created_at=datetime.now(timezone.utc),
            summary=summary,
            waybills=results,
        )

    # Header and fully materialised payload are committed together: an error
    # rolls everything back, so there is no half-written report, not even a
    # header row.
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO exposure_reports (party, at_utc, created_at, "
            "waybill_count, at_party_count, not_at_party_count, "
            "undeterminable_count, payload) VALUES (?, ?, ?, ?, ?, ?, ?, '')",
            (
                response.party,
                response.at_utc.strftime(_STORAGE_FMT),
                response.created_at.isoformat(),
                summary.total,
                summary.confirmed_at_party,
                summary.confirmed_not_at_party,
                summary.undeterminable,
            ),
        )
        report_id = int(cur.lastrowid)
        response.report_id = report_id
        conn.execute(
            "UPDATE exposure_reports SET payload = ? WHERE id = ?",
            (response.model_dump_json(), report_id),
        )

    return response


def _row_to_report(row: sqlite3.Row) -> ExposureReportResponse:
    report = ExposureReportResponse.model_validate_json(row["payload"])
    report.report_id = row["id"]
    return report


def _load_report(conn: sqlite3.Connection, report_id: int) -> ExposureReportResponse:
    row = conn.execute(
        "SELECT id, payload FROM exposure_reports WHERE id = ?", (report_id,)
    ).fetchone()
    if row is None:
        raise DomainValidation(f"exposure report {report_id} not found")
    return _row_to_report(row)


def get_exposure_report(
    conn: sqlite3.Connection, report_id: int
) -> ExposureReportResponse:
    """Fetch a stored report plus its difference from the current state.

    ``waybills`` / ``summary`` are exactly the conclusions committed with the
    report and never change afterwards.  ``changes`` is computed live against
    the current committed state (new waybills included), so the same response
    answers both "what did we conclude then" and "what differs now".
    """
    report = _load_report(conn, report_id)
    report.changes = compare_exposure_report(conn, report_id).changes
    return report


def list_exposure_reports(conn: sqlite3.Connection) -> ExposureReportList:
    rows = conn.execute(
        "SELECT id, party, at_utc, created_at, waybill_count, at_party_count, "
        "not_at_party_count, undeterminable_count "
        "FROM exposure_reports ORDER BY id"
    ).fetchall()
    return ExposureReportList(
        reports=[
            ExposureReportListItem(
                report_id=r["id"],
                party=r["party"],
                at_utc=_parse_ts(r["at_utc"]),
                created_at=datetime.fromisoformat(r["created_at"]),
                summary=ExposureSummary(
                    total=r["waybill_count"],
                    confirmed_at_party=r["at_party_count"],
                    confirmed_not_at_party=r["not_at_party_count"],
                    undeterminable=r["undeterminable_count"],
                ),
            )
            for r in rows
        ]
    )


def compare_exposure_report(
    conn: sqlite3.Connection, report_id: int
) -> ExposureChangesResponse:
    """Compare a stored report with the CURRENT committed state.

    * Stored conclusions are taken verbatim from the report payload (earlier
      reports never change when scans are later added/corrected/withdrawn).
    * The current side is evaluated from one read snapshot, so the comparison
      cannot mix revisions either.
    * Waybills that did not exist when the report was made appear as
      ``NEW_WAYBILL`` with their current conclusion.
    """
    from .database import read_snapshot

    stored = _load_report(conn, report_id)
    previous = {w.waybill_id: w for w in stored.waybills}

    with read_snapshot(conn):
        current_results, _ = _evaluate_report_from_snapshot(
            conn, stored.party, stored.at_utc.astimezone(timezone.utc)
        )

    changes: list[ExposureChange] = []
    for cur in current_results:
        prev = previous.get(cur.waybill_id)
        if prev is None:
            changes.append(
                ExposureChange(
                    waybill_id=cur.waybill_id,
                    change_type="NEW_WAYBILL",
                    is_new_waybill=True,
                    previous_classification=None,
                    current_classification=cur.classification,
                    new_classification=cur.classification,
                    previous_basis=None,
                    current_basis=cur.basis,
                )
            )
        elif prev.classification != cur.classification:
            changes.append(
                ExposureChange(
                    waybill_id=cur.waybill_id,
                    change_type="CLASSIFICATION_CHANGED",
                    is_new_waybill=False,
                    previous_classification=prev.classification,
                    current_classification=cur.classification,
                    new_classification=cur.classification,
                    previous_basis=prev.basis,
                    current_basis=cur.basis,
                )
            )

    return ExposureChangesResponse(
        report_id=stored.report_id,
        party=stored.party,
        at_utc=stored.at_utc,
        changes=changes,
    )


__all__ = [
    "DomainConflict",
    "DomainValidation",
    "apply_batch",
    "compare_exposure_report",
    "compute_chain",
    "create_waybill",
    "generate_exposure_report",
    "get_exposure_report",
    "get_waybill",
    "list_exposure_reports",
]
