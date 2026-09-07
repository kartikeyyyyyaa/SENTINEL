"""The rule engine — primitives in, alerts out.

The properties under test are the ones the rest of the system quietly depends
on:

* **Alert-key determinism.** The same (entry, camera, track) sighting must
  produce a byte-identical ``alert_key`` every time. The registry's whole
  upsert-on-conflict design (one operator-visible alert per sighting, retries
  and repeat frames collapsing rather than duplicating) rests on this being
  stable — so it is asserted directly here, at the source.
* **Risk maps to severity, not per-deployment.** An officer who sets a
  watchlist entry to "critical" has a right to expect it pages as "critical" in
  every district.
* **Cooldown suppresses, then re-fires.** A sustained condition must page once,
  not once per frame — and must page *again* once the window has elapsed, or a
  still-ongoing incident would go silent forever.
* **Speed enforcement uses the lower bound, not the point estimate** — an
  enforcement-adjacent decision must not spend the measurement's own error
  margin.

All fixtures are hand-built primitives; no camera, model or clock is involved.
``t`` is the monotonic cascade clock the cooldown windows are measured against,
so the tests drive it directly rather than sleeping.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from services.analytics.config import CameraConfig
from services.analytics.rules import RuleConfig, RuleEngine, _RISK_TO_SEVERITY
from services.analytics.watchlist import WatchlistEntry, WatchlistIndex
from services.analytics.stages.plate import normalise
from services.common.events import PrimitiveEvent

_TS = datetime(2026, 9, 4, 18, 0, 0, tzinfo=timezone.utc)  # 23:30 IST — "night"
_DAY_TS = datetime(2026, 9, 4, 6, 0, 0, tzinfo=timezone.utc)  # 11:30 IST — daytime


def _camera(camera_id: int = 1, label: str = "CG Road") -> CameraConfig:
    return CameraConfig(camera_id=camera_id, url="stub://", label=label)


def _engine(entries=(), config: RuleConfig | None = None) -> RuleEngine:
    return RuleEngine(config or RuleConfig(), WatchlistIndex(entries))


def _stolen(plate: str = "GJ01AB1234", risk: str = "critical") -> WatchlistEntry:
    return WatchlistEntry(
        entry_id="1",
        entry_type="stolen_vehicle",
        risk_level=risk,
        plate_number=normalise(plate),
        label="2019 white Swift",
        case_reference="FIR-AHM-2026-00417",
    )


def _anpr(plate: str, camera_id: int = 1, track_id: str = "t1", ts=_TS) -> PrimitiveEvent:
    return PrimitiveEvent(
        kind="anpr", camera_id=camera_id, ts=ts, track_id=track_id,
        payload={"plate_text": plate, "confidence": 0.9},
    )


class VehicleWatchlist(unittest.TestCase):
    def test_matching_plate_produces_one_alert(self):
        engine = _engine([_stolen()])
        alerts = engine.evaluate(_camera(), [_anpr("GJ01AB1234")], tracks=[], t=1.0)
        self.assertEqual(len(alerts), 1)
        a = alerts[0]
        self.assertEqual(a.kind, "watchlist_match_vehicle")
        self.assertEqual(a.detail["entry_id"], "1")
        self.assertEqual(a.detail["basis"], "plate")
        self.assertEqual(a.case_reference, "FIR-AHM-2026-00417")

    def test_non_listed_plate_produces_no_alert(self):
        engine = _engine([_stolen()])
        alerts = engine.evaluate(_camera(), [_anpr("GJ99ZZ0000")], tracks=[], t=1.0)
        self.assertEqual(alerts, [])

    def test_alert_key_is_deterministic_across_calls(self):
        # The property the registry's upsert depends on: identical sighting →
        # identical key, so a retry or a repeat frame collapses to one alert.
        engine = _engine([_stolen()])
        first = engine.evaluate(_camera(), [_anpr("GJ01AB1234")], tracks=[], t=1.0)[0]
        second = engine.evaluate(_camera(), [_anpr("GJ01AB1234")], tracks=[], t=2.0)[0]
        self.assertEqual(first.alert_key, second.alert_key)
        self.assertEqual(
            first.alert_key, "watchlist_match_vehicle:1:1:t1"
        )

    def test_alert_key_differs_by_camera_and_track(self):
        engine = _engine([_stolen()])
        cam2 = engine.evaluate(_camera(2), [_anpr("GJ01AB1234", camera_id=2)], tracks=[], t=1.0)[0]
        trk2 = engine.evaluate(_camera(), [_anpr("GJ01AB1234", track_id="t2")], tracks=[], t=1.0)[0]
        self.assertIn(":2:", cam2.alert_key)
        self.assertTrue(cam2.alert_key.endswith(":t1"))
        self.assertTrue(trk2.alert_key.endswith(":t2"))

    def test_risk_level_maps_to_severity(self):
        self.assertEqual(_RISK_TO_SEVERITY["critical"], "critical")
        self.assertEqual(_RISK_TO_SEVERITY["high"], "urgent")
        self.assertEqual(_RISK_TO_SEVERITY["medium"], "advisory")
        self.assertEqual(_RISK_TO_SEVERITY["low"], "info")
        engine = _engine([_stolen(risk="medium")])
        a = engine.evaluate(_camera(), [_anpr("GJ01AB1234")], tracks=[], t=1.0)[0]
        self.assertEqual(a.severity, "advisory")

    def test_anpr_event_with_no_plate_text_is_ignored(self):
        engine = _engine([_stolen()])
        evt = PrimitiveEvent(kind="anpr", camera_id=1, ts=_TS, track_id="t1", payload={})
        self.assertEqual(engine.evaluate(_camera(), [evt], tracks=[], t=1.0), [])


class Cooldown(unittest.TestCase):
    def _crowd(self, count=20, zone="z1"):
        return PrimitiveEvent(
            kind="crowd", camera_id=1, ts=_TS,
            payload={"all_tracks": count, "zone_id": zone},
        )

    def test_fires_then_suppresses_then_refires(self):
        engine = _engine(config=RuleConfig(crowd_alert_threshold=15, crowd_repeat_seconds=120.0))
        cam = _camera()
        # t=100: first crossing fires.
        self.assertEqual(len(engine.evaluate(cam, [self._crowd()], tracks=[], t=100.0)), 1)
        # t=150: within the 120s window, suppressed.
        self.assertEqual(len(engine.evaluate(cam, [self._crowd()], tracks=[], t=150.0)), 0)
        # t=250: window elapsed (>120s since last emit at 100), fires again.
        self.assertEqual(len(engine.evaluate(cam, [self._crowd()], tracks=[], t=250.0)), 1)

    def test_below_threshold_never_fires(self):
        engine = _engine(config=RuleConfig(crowd_alert_threshold=15))
        self.assertEqual(engine.evaluate(_camera(), [self._crowd(count=5)], tracks=[], t=1.0), [])

    def test_cooldown_is_per_camera(self):
        engine = _engine(config=RuleConfig(crowd_repeat_seconds=120.0))
        # Two cameras crossing at the same instant must both fire — the cooldown
        # key is namespaced by camera_id, so one must not suppress the other.
        a = engine.evaluate(_camera(1), [self._crowd()], tracks=[], t=100.0)
        b = engine.evaluate(_camera(2), [self._crowd()], tracks=[], t=100.0)
        self.assertEqual(len(a), 1)
        self.assertEqual(len(b), 1)


class Speed(unittest.TestCase):
    def _speed(self, lower_bound, kmph=90):
        return PrimitiveEvent(
            kind="speed", camera_id=1, ts=_TS, track_id="t1",
            payload={"lower_bound_kmph": lower_bound, "kmph": kmph},
        )

    def test_uses_lower_bound_not_point_estimate(self):
        engine = _engine(config=RuleConfig(speed_limit_kmph=60.0))
        # Point estimate 90 is over the limit, but the lower bound 55 is not —
        # so no violation is emitted. Spending the error margin here would put a
        # contestable speed on an enforcement-adjacent record.
        self.assertEqual(engine.evaluate(_camera(), [self._speed(lower_bound=55)], tracks=[], t=1.0), [])

    def test_lower_bound_over_limit_fires(self):
        engine = _engine(config=RuleConfig(speed_limit_kmph=60.0))
        alerts = engine.evaluate(_camera(), [self._speed(lower_bound=72)], tracks=[], t=1.0)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].kind, "speed_violation")


class StreamGap(unittest.TestCase):
    def _gap(self, seconds):
        return PrimitiveEvent(
            kind="stream_gap", camera_id=1, ts=_TS, payload={"gap_seconds": seconds, "reason": "timeout"},
        )

    def test_short_gap_below_threshold_is_ignored(self):
        engine = _engine(config=RuleConfig(stream_gap_alert_seconds=30.0))
        self.assertEqual(engine.evaluate(_camera(), [self._gap(10)], tracks=[], t=1.0), [])

    def test_prolonged_gap_fires(self):
        engine = _engine(config=RuleConfig(stream_gap_alert_seconds=30.0))
        alerts = engine.evaluate(_camera(), [self._gap(45)], tracks=[], t=1.0)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].kind, "stream_gap_prolonged")


class WomenSafety(unittest.TestCase):
    def _proximity(self, ids, labels=("person", "person")):
        return PrimitiveEvent(
            kind="proximity", camera_id=1, ts=_TS,
            payload={"track_ids": list(ids), "class_labels": list(labels), "seconds": 8},
        )

    def test_encircled_one_track_with_multiple_partners(self):
        # Track A is close to both B and C at once → surrounded.
        engine = _engine(config=RuleConfig(encircle_min_partners=2))
        events = [self._proximity(["A", "B"]), self._proximity(["A", "C"])]
        alerts = engine.evaluate(_camera(), events, tracks=[], t=1.0)
        risks = [a for a in alerts if a.kind == "women_safety_risk"]
        self.assertTrue(risks)
        encircled = [a for a in risks if a.detail.get("pattern") == "encircled"]
        self.assertEqual(len(encircled), 1)
        self.assertEqual(encircled[0].severity, "urgent")
        self.assertEqual(encircled[0].detail["partner_count"], 2)

    def test_single_proximity_pair_is_not_encircled(self):
        engine = _engine(config=RuleConfig(encircle_min_partners=2))
        alerts = engine.evaluate(_camera(), [self._proximity(["A", "B"])], tracks=[], t=1.0)
        encircled = [a for a in alerts if a.detail.get("pattern") == "encircled"]
        self.assertEqual(encircled, [])

    def test_no_proximity_events_no_safety_alert(self):
        engine = _engine()
        self.assertEqual(engine.evaluate(_camera(), [], tracks=[], t=1.0), [])


if __name__ == "__main__":
    unittest.main()
