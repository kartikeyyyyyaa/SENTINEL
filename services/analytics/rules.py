"""The correlation layer ``primitives.py`` and ``events.py`` describe but do not
implement: rules over primitives, authored as data, producing ``Alert``s.

Every incident type Sentinel supports before this module is a *primitive* —
"these two tracks stayed close", "a plate was read", "a region got crowded".
None of those is, by itself, something an operator should be paged for; that
judgement is what this module adds, and it adds it as configuration
(``RuleConfig``) over facts the rest of the pipeline already produces, not as a
new model or a new event kind. Concretely, one worker cycle's primitive events
plus the currently-live tracks go in; zero or more ``Alert``s come out.

**How a watchlist match becomes "start mapping the criminal from there".** A
``watchlist_match_vehicle`` alert carries the camera, the timestamp and the
matched plate. Two such alerts for the same watchlist entry from two different
cameras are, by construction, two sightings of the same vehicle at two points on
the map at two times — which is exactly a trail, built from data every alert
already carries, with no new "tracking" subsystem and no biometric identity
resolution required. ``services/registry`` is where those alerts accumulate
across the whole camera fleet and where an operator would query "everywhere
entry X has been seen today"; this module's job stops at emitting the first
alert honestly.

**Women's safety.** ``rule_women_safety`` composes exactly the primitives
``primitives.py`` already computes — ``proximity`` plus zone and time context —
and adds no appearance-based signal of any kind. Read the "deliberate omission"
note at the bottom of ``services/common/events.py`` before touching this
function; it is not incidental that the inputs here are geometry and a clock.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Mapping, Sequence

from services.common.alerts import Alert
from services.common.events import PrimitiveEvent

from .config import CameraConfig
from .tracker import Track
from .watchlist import WatchlistEntry, WatchlistIndex

log = logging.getLogger(__name__)

#: Watchlist risk_level -> alert severity. A fixed mapping, not configurable per
#: deployment: an officer setting a "critical" risk level on an entry has a right
#: to expect it always pages as "critical" regardless of which district's worker
#: happens to see it.
_RISK_TO_SEVERITY: Mapping[str, str] = {
    "critical": "critical",
    "high": "urgent",
    "medium": "advisory",
    "low": "info",
}

#: Gujarat is UTC+5:30 and has one timezone; a fixed offset is correct here in a
#: way it would not be for a system spanning zones, and it avoids taking a
#: dependency (zoneinfo's tzdata) for a single unchanging offset.
_IST_OFFSET = timedelta(hours=5, minutes=30)


def _ist_hour(ts: datetime) -> int:
    return (ts.astimezone(timezone.utc) + _IST_OFFSET).hour


@dataclass(frozen=True, slots=True)
class RuleConfig:
    """Thresholds for the correlation layer. Every value here is tunable without
    a redeploy, for the same reason ``PrimitiveConfig`` is: "page on crowds over
    40" and "page on crowds over 15" are the same rule with a different number."""

    # -- women's safety ------------------------------------------------------
    #: Hours (IST, inclusive-exclusive, wrapping midnight) treated as "after
    #: dark" for the followed-pattern rule. 21:00 to 05:00 by default.
    night_start_hour: int = 21
    night_end_hour: int = 5
    #: A proximity pair only escalates to a safety risk when at most this many
    #: tracks are live in the whole scene — i.e. the two of them are essentially
    #: alone, which is the condition that makes "followed" concerning rather
    #: than "walking through a crowd". See ``rule_women_safety``.
    lone_scene_track_ceiling: int = 3
    #: Distinct simultaneous proximity partners before a single track counts as
    #: "surrounded" rather than merely "close to one other track".
    encircle_min_partners: int = 2
    women_safety_repeat_seconds: float = 120.0

    # -- crowd -----------------------------------------------------------------
    crowd_alert_threshold: int = 15
    crowd_repeat_seconds: float = 120.0

    # -- speed -------------------------------------------------------------
    #: A single default limit. A per-camera or per-lane limit is a registry
    #: field this rule does not yet read; see the module docstring's stance on
    #: authoring extensions as data, not as new code, once that field exists.
    speed_limit_kmph: float = 60.0
    speed_repeat_seconds: float = 60.0

    # -- coverage ------------------------------------------------------------
    stream_gap_alert_seconds: float = 30.0
    stream_gap_repeat_seconds: float = 300.0

    @staticmethod
    def from_env() -> RuleConfig:
        from .config import _env_float, _env_int  # noqa: PLC0415 - avoid a cycle at import

        return RuleConfig(
            night_start_hour=_env_int("ANALYTICS_RULES_NIGHT_START_HOUR", 21),
            night_end_hour=_env_int("ANALYTICS_RULES_NIGHT_END_HOUR", 5),
            lone_scene_track_ceiling=_env_int("ANALYTICS_RULES_LONE_SCENE_CEILING", 3),
            encircle_min_partners=_env_int("ANALYTICS_RULES_ENCIRCLE_MIN_PARTNERS", 2),
            women_safety_repeat_seconds=_env_float("ANALYTICS_RULES_WOMEN_SAFETY_REPEAT_SECONDS", 120.0),
            crowd_alert_threshold=_env_int("ANALYTICS_RULES_CROWD_THRESHOLD", 15),
            crowd_repeat_seconds=_env_float("ANALYTICS_RULES_CROWD_REPEAT_SECONDS", 120.0),
            speed_limit_kmph=_env_float("ANALYTICS_RULES_SPEED_LIMIT_KMPH", 60.0),
            speed_repeat_seconds=_env_float("ANALYTICS_RULES_SPEED_REPEAT_SECONDS", 60.0),
            stream_gap_alert_seconds=_env_float("ANALYTICS_RULES_STREAM_GAP_ALERT_SECONDS", 30.0),
            stream_gap_repeat_seconds=_env_float("ANALYTICS_RULES_STREAM_GAP_REPEAT_SECONDS", 300.0),
        )


class RuleEngine:
    """Stateful per-worker; holds cooldown timers so a sustained condition pages
    once, not once per frame. One instance is shared by every camera in the
    process — cooldown keys are namespaced by ``camera_id`` — mirroring how
    ``main.Worker`` shares one detector and one sink across cameras.
    """

    def __init__(self, config: RuleConfig, watchlist: WatchlistIndex) -> None:
        self.config = config
        self.watchlist = watchlist
        self._last_emitted: dict[tuple, float] = {}

    def evaluate(
        self,
        camera: CameraConfig,
        events: Sequence[PrimitiveEvent],
        tracks: Sequence[Track],
        t: float,
    ) -> list[Alert]:
        """Run every rule over one cycle's primitives. ``t`` is the monotonic
        cascade clock (same one ``PrimitiveEngine`` uses), which is what cooldown
        windows are measured against — not wall time, so a dry run's compressed
        synthetic clock still produces correctly-spaced alerts."""
        alerts: list[Alert] = []
        alerts.extend(self._rule_watchlist_vehicle(camera, events, t))
        alerts.extend(self._rule_women_safety(camera, events, tracks, t))
        alerts.extend(self._rule_abandoned(camera, events))
        alerts.extend(self._rule_crowd(camera, events, t))
        alerts.extend(self._rule_speed(camera, events, t))
        alerts.extend(self._rule_stream_gap(camera, events, t))
        return alerts

    # -- vehicle watchlist ---------------------------------------------------

    def _rule_watchlist_vehicle(
        self, camera: CameraConfig, events: Sequence[PrimitiveEvent], t: float
    ) -> list[Alert]:
        out: list[Alert] = []
        for event in events:
            if event.kind != "anpr":
                continue
            plate = event.payload.get("plate_text")
            if not plate:
                continue
            entry = self.watchlist.match_plate(str(plate))
            if entry is None:
                continue
            out.append(self._vehicle_alert(camera, event, entry))
        return out

    def _vehicle_alert(self, camera: CameraConfig, event: PrimitiveEvent, entry: WatchlistEntry) -> Alert:
        return Alert(
            kind="watchlist_match_vehicle",
            severity=_RISK_TO_SEVERITY.get(entry.risk_level, "advisory"),
            camera_id=camera.camera_id,
            ts=event.ts,
            summary=(
                f"{entry.entry_type.replace('_', ' ')} matched at "
                f"{camera.label or camera.camera_id}: {event.payload.get('plate_text')}"
                + (f" ({entry.label})" if entry.label else "")
            ),
            alert_key=f"watchlist_match_vehicle:{entry.entry_id}:{camera.camera_id}:{event.track_id}",
            track_ids=(event.track_id,) if event.track_id else (),
            source_event_kinds=("anpr",),
            detail={
                "entry_id": entry.entry_id,
                "entry_type": entry.entry_type,
                "risk_level": entry.risk_level,
                "plate_text": event.payload.get("plate_text"),
                "basis": "plate",
            },
            case_reference=entry.case_reference,
        )

    def person_watchlist_alert(
        self, camera: CameraConfig, ts: datetime, track_id: str, entry: WatchlistEntry, similarity: float
    ) -> Alert:
        """Called directly by ``main.CameraWorker`` when the face stage produces
        a match — see the module docstring on ``watchlist.py`` for why face
        embeddings never travel as a primitive event and this path is separate
        from :meth:`evaluate`."""
        return Alert(
            kind="watchlist_match_person",
            severity=_RISK_TO_SEVERITY.get(entry.risk_level, "advisory"),
            camera_id=camera.camera_id,
            ts=ts,
            summary=(
                f"{entry.entry_type.replace('_', ' ')} matched at "
                f"{camera.label or camera.camera_id}"
                + (f": {entry.label}" if entry.label else "")
            ),
            alert_key=f"watchlist_match_person:{entry.entry_id}:{camera.camera_id}:{track_id}",
            track_ids=(track_id,),
            source_event_kinds=(),
            detail={
                "entry_id": entry.entry_id,
                "entry_type": entry.entry_type,
                "risk_level": entry.risk_level,
                "similarity": round(similarity, 4),
                "basis": "face",
            },
            case_reference=entry.case_reference,
        )

    # -- women's safety --------------------------------------------------------

    def _rule_women_safety(
        self,
        camera: CameraConfig,
        events: Sequence[PrimitiveEvent],
        tracks: Sequence[Track],
        t: float,
    ) -> list[Alert]:
        proximity_events = [e for e in events if e.kind == "proximity"]
        if not proximity_events:
            return []
        cfg = self.config
        out: list[Alert] = []

        # -- pattern 1: encircled — one track has multiple simultaneous partners
        partners: dict[str, set[str]] = {}
        for event in proximity_events:
            ids = event.payload.get("track_ids") or []
            if len(ids) != 2:
                continue
            a, b = ids
            partners.setdefault(a, set()).add(b)
            partners.setdefault(b, set()).add(a)
        for track_id, others in partners.items():
            if len(others) < cfg.encircle_min_partners:
                continue
            key = ("women_safety_encircled", camera.camera_id, track_id)
            if not self._due(key, t, cfg.women_safety_repeat_seconds):
                continue
            out.append(
                Alert(
                    kind="women_safety_risk",
                    severity="urgent",
                    camera_id=camera.camera_id,
                    ts=proximity_events[0].ts,
                    summary=(
                        f"Track surrounded by {len(others)} others at "
                        f"{camera.label or camera.camera_id}"
                    ),
                    alert_key=f"women_safety_encircled:{camera.camera_id}:{track_id}:{int(t)}",
                    track_ids=(track_id, *sorted(others)),
                    source_event_kinds=("proximity",),
                    detail={"pattern": "encircled", "partner_count": len(others)},
                )
            )

        # -- pattern 2: followed alone, after dark or in a flagged zone
        is_night = self._is_night(proximity_events[0].ts)
        isolated_zone_ids = {z.zone_id for z in camera.zones if getattr(z, "isolated", False)}
        active_zone_ids = {
            e.payload.get("zone_id") for e in events if e.kind == "zone_enter" and e.payload.get("zone_id")
        }
        in_isolated_zone = bool(isolated_zone_ids & active_zone_ids)
        if (is_night or in_isolated_zone) and len(tracks) <= cfg.lone_scene_track_ceiling:
            for event in proximity_events:
                labels = event.payload.get("class_labels") or []
                if "person" not in labels:
                    continue
                ids = tuple(event.payload.get("track_ids") or ())
                if not ids:
                    continue
                key = ("women_safety_followed", camera.camera_id, tuple(sorted(ids)))
                if not self._due(key, t, cfg.women_safety_repeat_seconds):
                    continue
                out.append(
                    Alert(
                        kind="women_safety_risk",
                        severity="urgent" if (is_night and in_isolated_zone) else "advisory",
                        camera_id=camera.camera_id,
                        ts=event.ts,
                        summary=(
                            f"Lone person followed at {camera.label or camera.camera_id}"
                            + (" (isolated zone)" if in_isolated_zone else "")
                            + (" (after dark)" if is_night else "")
                        ),
                        alert_key=f"women_safety_followed:{camera.camera_id}:{sorted(ids)}:{int(t)}",
                        track_ids=ids,
                        source_event_kinds=("proximity", "zone_enter"),
                        detail={
                            "pattern": "followed",
                            "is_night": is_night,
                            "in_isolated_zone": in_isolated_zone,
                            "seconds": event.payload.get("seconds"),
                            "mean_heading_correlation": event.payload.get("mean_heading_correlation"),
                        },
                    )
                )
        return out

    def _is_night(self, ts: datetime) -> bool:
        cfg = self.config
        hour = _ist_hour(ts)
        if cfg.night_start_hour <= cfg.night_end_hour:
            return cfg.night_start_hour <= hour < cfg.night_end_hour
        return hour >= cfg.night_start_hour or hour < cfg.night_end_hour  # wraps past midnight

    # -- abandoned / crowd / speed / coverage --------------------------------

    def _rule_abandoned(self, camera: CameraConfig, events: Sequence[PrimitiveEvent]) -> list[Alert]:
        out = []
        for event in events:
            if event.kind != "abandoned":
                continue
            out.append(
                Alert(
                    kind="abandoned_object",
                    severity="advisory",
                    camera_id=camera.camera_id,
                    ts=event.ts,
                    summary=f"Unattended {event.payload.get('class_label', 'object')} at "
                    f"{camera.label or camera.camera_id}",
                    alert_key=f"abandoned_object:{camera.camera_id}:{event.track_id}",
                    track_ids=(event.track_id,) if event.track_id else (),
                    source_event_kinds=("abandoned",),
                    detail=dict(event.payload),
                )
            )
        return out

    def _rule_crowd(self, camera: CameraConfig, events: Sequence[PrimitiveEvent], t: float) -> list[Alert]:
        cfg = self.config
        out = []
        for event in events:
            if event.kind != "crowd":
                continue
            count = int(event.payload.get("all_tracks", 0))
            if count < cfg.crowd_alert_threshold:
                continue
            zone_id = event.payload.get("zone_id", "")
            key = ("crowd_surge", camera.camera_id, zone_id)
            if not self._due(key, t, cfg.crowd_repeat_seconds):
                continue
            out.append(
                Alert(
                    kind="crowd_surge",
                    severity="advisory",
                    camera_id=camera.camera_id,
                    ts=event.ts,
                    summary=f"Crowd density {count} tracks in zone {zone_id} at "
                    f"{camera.label or camera.camera_id}",
                    alert_key=f"crowd_surge:{camera.camera_id}:{zone_id}:{int(t)}",
                    source_event_kinds=("crowd",),
                    detail=dict(event.payload),
                )
            )
        return out

    def _rule_speed(self, camera: CameraConfig, events: Sequence[PrimitiveEvent], t: float) -> list[Alert]:
        cfg = self.config
        out = []
        for event in events:
            if event.kind != "speed":
                continue
            lower_bound = event.payload.get("lower_bound_kmph")
            if lower_bound is None or float(lower_bound) < cfg.speed_limit_kmph:
                # Compared against the lower bound, not the point estimate — see
                # SpeedEstimator's docstring on why an enforcement-adjacent
                # decision must not spend the measurement's own error margin.
                continue
            key = ("speed_violation", camera.camera_id, event.track_id)
            if not self._due(key, t, cfg.speed_repeat_seconds):
                continue
            out.append(
                Alert(
                    kind="speed_violation",
                    severity="info",
                    camera_id=camera.camera_id,
                    ts=event.ts,
                    summary=f"~{event.payload.get('kmph')} km/h (limit {cfg.speed_limit_kmph}) at "
                    f"{camera.label or camera.camera_id}",
                    alert_key=f"speed_violation:{camera.camera_id}:{event.track_id}:{int(t)}",
                    track_ids=(event.track_id,) if event.track_id else (),
                    source_event_kinds=("speed",),
                    detail=dict(event.payload),
                )
            )
        return out

    def _rule_stream_gap(self, camera: CameraConfig, events: Sequence[PrimitiveEvent], t: float) -> list[Alert]:
        cfg = self.config
        out = []
        for event in events:
            if event.kind != "stream_gap":
                continue
            gap = float(event.payload.get("gap_seconds", 0.0))
            if gap < cfg.stream_gap_alert_seconds:
                continue
            key = ("stream_gap_prolonged", camera.camera_id)
            if not self._due(key, t, cfg.stream_gap_repeat_seconds):
                continue
            out.append(
                Alert(
                    kind="stream_gap_prolonged",
                    severity="advisory",
                    camera_id=camera.camera_id,
                    ts=event.ts,
                    summary=f"{camera.label or camera.camera_id} lost {gap:.0f}s of coverage",
                    alert_key=f"stream_gap_prolonged:{camera.camera_id}:{int(t)}",
                    source_event_kinds=("stream_gap",),
                    detail=dict(event.payload),
                )
            )
        return out

    # -- plumbing --------------------------------------------------------------

    def _due(self, key: tuple, t: float, repeat_seconds: float) -> bool:
        last = self._last_emitted.get(key, float("-inf"))
        if (t - last) < repeat_seconds:
            return False
        self._last_emitted[key] = t
        return True
