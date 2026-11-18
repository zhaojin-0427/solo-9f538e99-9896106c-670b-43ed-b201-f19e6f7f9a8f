"""Pydantic request/response models and payload canonicalization."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #
class WaybillCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    waybill_id: str = Field(min_length=1, description="运单号，全局唯一")
    origin_party: str = Field(min_length=1, description="固定起始保管方")

    @field_validator("waybill_id", "origin_party")
    @classmethod
    def _strip_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v


class ScanEntry(BaseModel):
    """One scan inside a batch submission.

    ``event_type`` defaults to HANDOVER so the common case only carries
    scan_id / ts_utc / giver / receiver / revision.
    """

    model_config = ConfigDict(extra="forbid")

    scan_id: str = Field(min_length=1)
    revision: int = Field(gt=0, description="正整数修订号；同 ID 仅最高修订生效")
    event_type: Literal["HANDOVER", "WITHDRAWAL"] = "HANDOVER"
    ts_utc: datetime | None = Field(
        default=None, description="ISO-8601，必须带时区偏移（推荐 UTC，如 2026-11-18T08:30:00Z）"
    )
    giver: str | None = None
    receiver: str | None = None

    @field_validator("scan_id")
    @classmethod
    def _strip_scan_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v

    @field_validator("giver", "receiver")
    @classmethod
    def _strip_parties(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("ts_utc")
    @classmethod
    def _require_timezone(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("ts_utc must carry an explicit timezone offset, e.g. ...Z or +08:00")
        return v

    @model_validator(mode="after")
    def _check_shape(self) -> "ScanEntry":
        if self.event_type == "HANDOVER":
            missing = [
                name
                for name, val in (
                    ("ts_utc", self.ts_utc),
                    ("giver", self.giver),
                    ("receiver", self.receiver),
                )
                if val is None
            ]
            if missing:
                raise ValueError(f"HANDOVER entry requires fields: {', '.join(missing)}")
        else:  # WITHDRAWAL: only id + higher revision are accepted
            extras = [
                name
                for name, val in (
                    ("ts_utc", self.ts_utc),
                    ("giver", self.giver),
                    ("receiver", self.receiver),
                )
                if val is not None
            ]
            if extras:
                raise ValueError(
                    f"WITHDRAWAL entry must only carry scan_id and revision; got extra: {', '.join(extras)}"
                )
        return self


class BatchSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scans: list[ScanEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_batch_duplicates(self) -> "BatchSubmit":
        # Same revision appearing twice in one batch with different text is a
        # whole-batch rejection; identical text is treated as idempotent.
        seen: dict[tuple[str, int], str] = {}
        for entry in self.scans:
            key = (entry.scan_id, entry.revision)
            h = entry_payload_hash(entry)
            if key in seen and seen[key] != h:
                raise ValueError(
                    f"scan_id={key[0]} revision={key[1]} appears multiple times "
                    "with conflicting content in the same batch"
                )
            seen[key] = h
        return self


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #
class BreakReason(str, Enum):
    none = "NONE"
    gap = "GAP"  # 保管方不承接：giver != 前一 receiver
    simultaneous = "SIMULTANEOUS"  # 同一时刻多笔交接，顺序不可判定


class ScanView(BaseModel):
    scan_id: str
    revision: int
    event_type: Literal["HANDOVER", "WITHDRAWAL"]
    ts_utc: datetime | None
    giver: str | None
    receiver: str | None
    verified: bool
    unverified_reason: BreakReason | None = None


class ChainResponse(BaseModel):
    waybill_id: str
    origin_party: str
    current_custodian: str
    verified_prefix: list[ScanView]
    unverified_scans: list[ScanView]
    chain_intact: bool
    break_reason: BreakReason
    break_detail: str | None = None
    break_scan_ids: list[str] = Field(default_factory=list)


class BatchResponse(BaseModel):
    waybill_id: str
    applied: int
    idempotent_duplicates: int
    withdrawn_scan_ids: list[str]
    chain: ChainResponse


# --------------------------------------------------------------------------- #
# Exposure reports
# --------------------------------------------------------------------------- #
class ExposureReportCreate(BaseModel):
    """Request body for generating an exposure report.

    ``party`` is the custodian being investigated and ``at_utc`` the instant
    (an explicit timezone offset is mandatory, e.g. ``2026-11-18T09:30:00Z``).
    Common alternative spellings are accepted so the endpoint is convenient
    to drive from Swagger UI, but the canonical names are ``party``/``at_utc``.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    party: str = Field(
        min_length=1,
        validation_alias=AliasChoices(
            "party", "custodian", "party_name", "custodian_party", "target_party"
        ),
        description="被核查的保管方",
    )
    at_utc: datetime = Field(
        validation_alias=AliasChoices(
            "at_utc", "at", "instant_utc", "timestamp", "ts_utc",
            "time_utc", "instant", "moment",
        ),
        description="核查时刻（UTC 或带时区偏移）",
    )

    @field_validator("party")
    @classmethod
    def _strip_party(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("party must not be empty")
        return v

    @field_validator("at_utc")
    @classmethod
    def _require_timezone(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError(
                "at_utc must carry an explicit timezone offset, e.g. ...Z or +08:00"
            )
        return v


class ExposureClassification(str, Enum):
    """结论：确认在该方 / 确认不在该方 / 无法判定。"""

    at_party = "CONFIRMED_AT_PARTY"
    not_at_party = "CONFIRMED_NOT_AT_PARTY"
    undeterminable = "UNDETERMINABLE"


class ExposureBasis(BaseModel):
    """What the conclusion was derived from.

    ``kind=ORIGIN``: the instant precedes the first handover, so the fixed
    origin party is used (no scan).  ``kind=HANDOVER``: custody is the receiver
    of the last verified handover at or before the instant.  ``kind=BREAK``:
    the instant is at/after the chain break, so custody cannot be presumed;
    ``break_scan_ids`` / ``break_reason`` / ``break_detail`` locate the break.
    """

    kind: Literal["ORIGIN", "HANDOVER", "BREAK"]
    scan_id: str | None = None
    revision: int | None = None
    ts_utc: datetime | None = None
    custodian: str | None = None
    origin_party: str
    break_reason: BreakReason | None = None
    break_scan_ids: list[str] = Field(default_factory=list)
    break_detail: str | None = None


class ExposureWaybillResult(BaseModel):
    waybill_id: str
    origin_party: str
    classification: ExposureClassification
    conclusion: Literal["AT_PARTY", "NOT_AT_PARTY", "UNDETERMINABLE"] | None = Field(
        default=None,
        description="classification 的简写形式（AT_PARTY / NOT_AT_PARTY / UNDETERMINABLE）",
    )
    custodian: str | None = Field(
        default=None, description="该时刻可确认的保管方；无法判定时为 null"
    )
    basis: ExposureBasis
    # Flattened convenience views of the same basis (kept for easy filtering).
    basis_scan_id: str | None = None
    basis_revision: int | None = None
    basis_ts_utc: datetime | None = None
    break_reason: BreakReason | None = None
    break_scan_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _fill_flattened(self) -> "ExposureWaybillResult":
        if self.conclusion is None:
            self.conclusion = _SHORT_CONCLUSION[self.classification]
        if self.basis_scan_id is None:
            self.basis_scan_id = self.basis.scan_id
        if self.basis_revision is None:
            self.basis_revision = self.basis.revision
        if self.basis_ts_utc is None:
            self.basis_ts_utc = self.basis.ts_utc
        if self.break_reason is None:
            self.break_reason = self.basis.break_reason
        if not self.break_scan_ids:
            self.break_scan_ids = list(self.basis.break_scan_ids)
        return self


_SHORT_CONCLUSION = {
    ExposureClassification.at_party: "AT_PARTY",
    ExposureClassification.not_at_party: "NOT_AT_PARTY",
    ExposureClassification.undeterminable: "UNDETERMINABLE",
}


class ExposureSummary(BaseModel):
    total: int
    confirmed_at_party: int
    confirmed_not_at_party: int
    undeterminable: int


class ExposureChange(BaseModel):
    """A waybill whose classification differs from the stored report."""

    waybill_id: str
    change_type: Literal["CLASSIFICATION_CHANGED", "NEW_WAYBILL"]
    is_new_waybill: bool
    previous_classification: ExposureClassification | None = None
    current_classification: ExposureClassification
    new_classification: ExposureClassification = Field(
        description="同 current_classification，便于按“新结论”直接读取"
    )
    previous_basis: ExposureBasis | None = None
    current_basis: ExposureBasis


class ExposureReportResponse(BaseModel):
    report_id: int
    party: str
    at_utc: datetime
    created_at: datetime
    summary: ExposureSummary
    waybills: list[ExposureWaybillResult]
    changes: list[ExposureChange] = Field(
        default_factory=list,
        description="与当前已提交状态相比分类发生变化的运单；新增运单也计入",
    )


class ExposureReportListItem(BaseModel):
    report_id: int
    party: str
    at_utc: datetime
    created_at: datetime
    summary: ExposureSummary


class ExposureChangesResponse(BaseModel):
    report_id: int
    party: str
    at_utc: datetime
    changes: list[ExposureChange]


class ExposureReportList(BaseModel):
    reports: list[ExposureReportListItem]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_utc_iso(dt: datetime) -> str:
    """Normalize any timezone-aware datetime to a fixed-width UTC ISO string."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical_payload(entry: ScanEntry) -> dict:
    """Deterministic representation used to detect same-revision divergence."""
    if entry.event_type == "WITHDRAWAL":
        return {"event_type": "WITHDRAWAL"}
    return {
        "event_type": "HANDOVER",
        "ts_utc": to_utc_iso(entry.ts_utc),  # type: ignore[arg-type]
        "giver": entry.giver,
        "receiver": entry.receiver,
    }


def payload_hash_of(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def entry_payload_hash(entry: ScanEntry) -> str:
    return payload_hash_of(canonical_payload(entry))
