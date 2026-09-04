"""Request and response models for the HTTP API.

**Why these vocabularies are re-declared here rather than imported.** The
alert/event "kind" and "severity" vocabularies mirror
``services/common/alerts.py`` and ``services/common/events.py`` byte for byte,
but this service's Docker build context is ``services/registry`` alone (see
``Dockerfile`` and ``docker-compose.yml``) — it does not, and should not,
depend on a package that ships on edge boxes it will never run on. The two
sides of this boundary are independent deployables that agree on a JSON wire
contract, not two modules sharing Python types; each validates that contract on
its own side, the same way the edge worker validates a registry response it
receives instead of trusting its shape. If a kind or field is added to one, it
is added to both, deliberately, as a wire-contract change — not refactored into
a shared import that would force one process's dependency set onto the other.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user: "UserSummary"


class RefreshRequest(BaseModel):
    refresh_token: str


class UserSummary(BaseModel):
    id: int
    username: str
    full_name: str
    jurisdiction_path: str
    department_id: int | None
    is_statewide: bool
    permissions: list[str]


# ---------------------------------------------------------------------------
# Cameras
# ---------------------------------------------------------------------------


class CameraOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    department_id: int
    jurisdiction_id: int
    camera_type: str
    status: str
    lat: float | None = None
    lon: float | None = None
    address: str | None = None
    landmark: str | None = None


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

WatchlistEntryType = Literal[
    "stolen_vehicle", "blacklisted_vehicle", "wanted_person", "missing_person", "suspect"
]
RiskLevel = Literal["low", "medium", "high", "critical"]
WatchlistStatus = Literal["active", "resolved", "expired"]


class WatchlistEntryCreate(BaseModel):
    entry_type: WatchlistEntryType
    risk_level: RiskLevel = "medium"
    plate_number: str | None = Field(default=None, max_length=15)
    label: str = ""
    case_reference: str | None = None
    notes: str | None = None
    department_id: int | None = None
    jurisdiction_id: int
    expires_at: datetime | None = None

    # A photo never crosses this API — see 007_watchlist.sql's comment on
    # person_embedding. Enrollment of a face happens out of band (a police
    # photo processed once, offline, by whichever tool runs the embedding
    # model) and only the resulting vector is ever posted here.
    person_embedding: list[float] | None = None
    embedding_model: str | None = None


class WatchlistEntryUpdate(BaseModel):
    risk_level: RiskLevel | None = None
    status: WatchlistStatus | None = None
    label: str | None = None
    notes: str | None = None
    case_reference: str | None = None
    expires_at: datetime | None = None


class WatchlistEntryOut(BaseModel):
    id: int
    entry_type: str
    risk_level: str
    status: str
    plate_number: str | None
    label: str
    case_reference: str | None
    notes: str | None
    department_id: int | None
    jurisdiction_id: int
    source: str
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime
    has_face_embedding: bool = False


class WatchlistMatchOut(BaseModel):
    id: int
    entry_id: int
    camera_id: int | None
    track_id: str | None
    basis: str
    confidence: float | None
    raw_value: str | None
    matched_at: datetime
    alert_id: int | None


# The worker's periodic GET /api/analytics/watchlist pull. Shaped to match what
# services/analytics/watchlist.py's entry_from_mapping() already tolerates.
class WatchlistPullEntry(BaseModel):
    id: int
    entry_type: str
    plate_number: str | None = None
    person_embedding: list[float] | None = None
    embedding_model: str | None = None
    risk_level: str
    label: str
    case_reference: str | None = None


class WatchlistPullResponse(BaseModel):
    entries: list[WatchlistPullEntry]
    generated_at: datetime


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

AlertKind = Literal[
    "watchlist_match_vehicle",
    "watchlist_match_person",
    "women_safety_risk",
    "abandoned_object",
    "crowd_surge",
    "speed_violation",
    "sos",
    "stream_gap_prolonged",
]
Severity = Literal["info", "advisory", "urgent", "critical"]
AlertStatus = Literal["open", "acknowledged", "closed", "false_positive"]


class AlertIngest(BaseModel):
    """What an edge worker's AlertHttpTransport posts. One alert per call."""

    kind: AlertKind
    severity: Severity
    camera_id: int | None = None
    ts: datetime
    summary: str
    alert_key: str
    track_ids: list[str] = Field(default_factory=list)
    source_event_kinds: list[str] = Field(default_factory=list)
    detail: dict[str, Any] = Field(default_factory=dict)
    case_reference: str | None = None
    # Populated only for watchlist_match_* kinds, so the ingestion handler can
    # also append to app.watchlist_match without a second round trip.
    watchlist_entry_id: int | None = None
    match_basis: Literal["plate", "face"] | None = None
    match_confidence: float | None = None
    match_raw_value: str | None = None
    match_track_id: str | None = None


class AlertOut(BaseModel):
    id: int
    kind: str
    severity: str
    status: str
    camera_id: int | None
    jurisdiction_path: str | None
    alert_key: str
    track_ids: list[str]
    source_event_kinds: list[str]
    summary: str
    detail: dict[str, Any]
    case_reference: str | None
    opened_at: datetime
    acknowledged_at: datetime | None
    closed_at: datetime | None
    close_reason: str | None


class AlertAcknowledge(BaseModel):
    pass


class AlertClose(BaseModel):
    reason: str
    false_positive: bool = False


# ---------------------------------------------------------------------------
# Women-safety SOS — see 007_watchlist.sql's module comment: no login required.
# ---------------------------------------------------------------------------


class SosCreate(BaseModel):
    channel: Literal["kiosk", "mobile_app", "operator", "helpline"]
    camera_id: int | None = None
    jurisdiction_id: int | None = None
    lat: float | None = None
    lon: float | None = None
    notes: str | None = Field(default=None, max_length=2000)


class SosOut(BaseModel):
    id: int
    channel: str
    camera_id: int | None
    reported_at: datetime
    alert_id: int | None
    resolved_at: datetime | None


# ---------------------------------------------------------------------------
# Analytics event ingestion — broadcast-only, not persisted (see routers/analytics.py)
# ---------------------------------------------------------------------------


class AnalyticsEventIngest(BaseModel):
    kind: str
    camera_id: int | None = None
    ts: datetime
    track_ids: list[str] = Field(default_factory=list)
    detail: dict[str, Any] = Field(default_factory=dict)
