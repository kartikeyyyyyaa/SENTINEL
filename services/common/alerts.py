"""Alerts: a rule's opinion about a sequence of primitives.

``services.common.events`` deliberately stops at *primitives* — "these two
tracks stayed close for six seconds", "this plate was read" — and says
explicitly that turning a pattern of primitives into an operational judgement is
the correlation layer's job, "authored as data", not hard-coded into the wire
format. This module is that judgement's shape.

The split matters for the same reason ``Sighting`` is not a "conclusion" in
``events.py``: an ``Alert`` can be wrong (a false positive), acknowledged,
escalated, or closed, and none of that should ever require rewriting the
primitive facts that led to it. A primitive is immutable history; an alert has a
lifecycle (``AlertStatus``) on top of it. Keeping them as two objects, joined by
``source_event_kinds`` / ``source_track_ids`` rather than merged into one, is
what lets an operator see *both* — the judgement, and the raw evidence it was
made from — which is exactly what "explainable, auditable analytics" has to mean
in practice rather than as a slide bullet.

Nothing here is a model. Every alert kind below is produced by
``services.analytics.rules.RuleEngine`` from primitives that ``PrimitiveEngine``
and the plate/OCR stages already emit, or from an external signal
(``sos``) that never touched a camera at all. Adding a new rule is adding a
function to that module and, if it needs one, a new *kind* here — never a new
model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

#: Closed vocabulary, matching ``app.alert.kind``'s CHECK constraint in
#: ``db/migrations/007_watchlist.sql``. Kept closed for the same reason
#: ``PRIMITIVE_KINDS`` is: an unknown value is a data problem to surface loudly,
#: not a string the console has to guess how to render.
ALERT_KINDS = frozenset({
    #: An ANPR read matched an active ``stolen_vehicle`` or ``blacklisted_vehicle``
    #: watchlist entry. Basis is always ``plate`` — see ``watchlist.py``.
    "watchlist_match_vehicle",
    #: A face embedding computed from a person track matched an active
    #: ``wanted_person``, ``missing_person`` or ``suspect`` entry.
    "watchlist_match_person",
    #: The behavioural, non-demographic pattern described in
    #: ``services.analytics.rules.rule_women_safety`` fired: a lone person
    #: followed, or surrounded by a group, in a flagged zone or after dark.
    "women_safety_risk",
    #: A static vehicle or unclassified object persisted with no owner nearby.
    "abandoned_object",
    #: A crowd region's track count crossed an operator-set threshold.
    "crowd_surge",
    #: A speed estimate's lower bound exceeded the posted limit.
    "speed_violation",
    #: A citizen- or operator-triggered panic signal, unrelated to any camera.
    "sos",
    #: A camera has been unreachable for longer than the operational threshold —
    #: not a crime signal, but a coverage gap an operator must know about.
    "stream_gap_prolonged",
})

#: Ordered low to high. Kept as strings rather than an IntEnum because the value
#: crosses into JSON, SQL and the console constantly; ``SEVERITY_RANK`` below is
#: the escape hatch for the few places ordering actually matters (sorting,
#: "has this alert been downgraded").
SEVERITIES = ("info", "advisory", "urgent", "critical")
SEVERITY_RANK: Mapping[str, int] = {s: i for i, s in enumerate(SEVERITIES)}

#: Alert lifecycle. Mirrors ``app.alert.status``'s CHECK constraint.
ALERT_STATUSES = frozenset({"open", "acknowledged", "closed", "false_positive"})


@dataclass(frozen=True, slots=True)
class Alert:
    """One rule firing, ready to cross the edge boundary or be inserted.

    ``alert_key`` is a deterministic idempotency key — see
    ``RuleEngine`` for how each rule builds it — so that a retried delivery (the
    same failure mode ``sink.HttpTransport`` already handles for primitives)
    upserts rather than duplicates. It is not a database id: the registry assigns
    that on first insert and the worker never learns or needs it.
    """

    kind: str
    severity: str
    camera_id: int | None
    ts: datetime
    summary: str
    alert_key: str
    track_ids: tuple[str, ...] = ()
    #: Which primitive kinds fed this judgement, e.g. ``("proximity", "dwell")``.
    #: Provenance, not behaviour — see the module docstring.
    source_event_kinds: tuple[str, ...] = ()
    detail: Mapping[str, Any] = field(default_factory=dict)
    case_reference: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ALERT_KINDS:
            raise ValueError(
                f"unknown alert kind {self.kind!r}. Alerts are produced by rules in "
                f"services.analytics.rules from existing primitives; add the kind "
                f"here first if a new rule legitimately needs it."
            )
        if self.severity not in SEVERITY_RANK:
            raise ValueError(f"unknown severity {self.severity!r}; expected one of {SEVERITIES}")
        if self.ts.tzinfo is None:
            raise ValueError("Alert.ts must be timezone-aware UTC")


def alert_to_dict(alert: Alert) -> dict[str, Any]:
    """``Alert`` as a JSON-safe mapping. See ``sink.event_to_dict`` — same shape
    of function, same reason: the wire format is a transport concern, not a
    property of the dataclass that is also the shared contract."""
    return {
        "kind": alert.kind,
        "severity": alert.severity,
        "camera_id": alert.camera_id,
        "ts": alert.ts.isoformat(),
        "summary": alert.summary,
        "alert_key": alert.alert_key,
        "track_ids": list(alert.track_ids),
        "source_event_kinds": list(alert.source_event_kinds),
        "detail": dict(alert.detail),
        "case_reference": alert.case_reference,
    }
