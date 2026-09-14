"""Model 3 federation layer: the parser's tolerance, and its limits.

Two things are being tested here, and only one of them is ordinary.

The ordinary one is the driver contract: lookup, refusals, descriptor invariants.

The other is ``sentinel_grid.parse_catalogue``, which is deliberately permissive
because the sandbox's JSON shape has not been observed against a live instance.
Permissive parsers fail in one specific way — they accept something that has
quietly changed meaning and produce plausible garbage. So the tests below spend
most of their effort on the boundary: which shapes must parse, and which must be
*rejected* rather than tolerated. ``test_envelope_is_not_mistaken_for_a_camera``
and ``test_non_url_source_is_not_treated_as_a_url`` are the ones that matter; the
happy-path shapes are just the fixtures needed to reach them.

No network. The catalogue parser is pure, and the RTSP probe's socket layer is
substituted, so this file runs on a machine with nothing installed:

    cd services/registry && python -m unittest tests.test_federation -v

``httpx`` in particular is *not* required. Both drivers import it lazily inside the
method that needs a client, which is asserted below rather than assumed.
"""
from __future__ import annotations

import asyncio
import socket
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest import mock

from app.federation import base, genetec, milestone, onvif_rtsp, registry, sentinel_grid
from app.federation.base import (
    DriverHealth,
    FederationError,
    ForeignCamera,
    NotSupported,
    PtzCommand,
    StreamDescriptor,
    VmsDriver,
)


def run(coro: Any) -> Any:
    """Drive one coroutine to completion.

    ``asyncio.run`` rather than ``IsolatedAsyncioTestCase`` so each test's event
    loop is unmistakably its own and nothing leaks between them.
    """
    return asyncio.run(coro)


SANDBOX = {"base_url": "http://sandbox.example:8080"}


def grid(**overrides: Any) -> sentinel_grid.SentinelGridDriver:
    config = dict(SANDBOX)
    config.update(overrides)
    return sentinel_grid.SentinelGridDriver(config)


# ---------------------------------------------------------------------------
# Catalogue shapes
# ---------------------------------------------------------------------------


class CatalogueShapeTolerance(unittest.TestCase):
    """Which top-level shapes ``/api/ingest`` may return and still be understood."""

    def test_top_level_list(self) -> None:
        cameras = sentinel_grid.parse_catalogue(
            [
                {"id": "cam-1", "name": "Nehru Bridge", "lat": 23.02, "lon": 72.57},
                {"id": "cam-2", "name": "CG Road", "lat": 23.03, "lon": 72.55},
            ]
        )
        self.assertEqual([c.external_id for c in cameras], ["cam-1", "cam-2"])
        self.assertEqual(cameras[0].name, "Nehru Bridge")

    def test_every_documented_wrapper_key(self) -> None:
        entry = {"id": "cam-1", "lat": 23.0, "lon": 72.0}
        for key in ("cameras", "streams", "data", "items", "results", "feeds"):
            with self.subTest(wrapper=key):
                cameras = sentinel_grid.parse_catalogue({key: [entry]})
                self.assertEqual(len(cameras), 1)
                self.assertEqual(cameras[0].external_id, "cam-1")

    def test_wrapper_alongside_unrelated_envelope_fields(self) -> None:
        # Pagination and status fields around the payload must not confuse it.
        cameras = sentinel_grid.parse_catalogue(
            {"status": "ok", "count": 1, "cameras": [{"id": "cam-1"}]}
        )
        self.assertEqual(len(cameras), 1)

    def test_doubly_nested_wrapper(self) -> None:
        cameras = sentinel_grid.parse_catalogue({"data": {"cameras": [{"id": "cam-9"}]}})
        self.assertEqual([c.external_id for c in cameras], ["cam-9"])

    def test_dict_keyed_by_camera_id(self) -> None:
        # MediaMTX's path listing is dict-shaped in some versions; the key is the
        # only place the stream name appears.
        cameras = sentinel_grid.parse_catalogue(
            {
                "stream_01": {"source": "rtsp://origin.example/live", "ready": True},
                "stream_02": {"source": "rtsp://origin.example/live2", "ready": False},
            }
        )
        self.assertEqual(
            sorted(c.external_id for c in cameras), ["stream_01", "stream_02"]
        )

    def test_dict_keyed_by_id_does_not_overwrite_an_explicit_id(self) -> None:
        cameras = sentinel_grid.parse_catalogue({"ignored-key": {"id": "real-id"}})
        self.assertEqual(cameras[0].external_id, "real-id")

    def test_single_object_is_accepted_as_a_collection_of_one(self) -> None:
        cameras = sentinel_grid.parse_catalogue({"id": "only-cam", "lat": 1.0, "lon": 2.0})
        self.assertEqual([c.external_id for c in cameras], ["only-cam"])

    def test_envelope_is_not_mistaken_for_a_camera(self) -> None:
        """The failure mode tolerance invites: inventing cameras out of structure.

        ``{"data": {}}`` is a dict of one dict, which the id-keyed branch would
        happily fold into a camera called "data". A phantom camera is worse than a
        parse error: it reaches the map, an operator clicks it, and the platform
        looks broken in a way nobody can trace back to here.
        """
        for payload in (
            {"data": {}},
            {"cameras": {}},
            {"meta": {"page": 1}, "links": {"next": None}},
            {"error": {"message": "nope"}},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(sentinel_grid.parse_catalogue(payload), [])

    def test_empty_catalogue_is_not_an_error(self) -> None:
        # A grid with nothing running yet is a normal state on demo morning.
        for payload in ([], {}, {"cameras": []}, {"data": []}):
            with self.subTest(payload=payload):
                self.assertEqual(sentinel_grid.parse_catalogue(payload), [])

    def test_non_container_payload_is_a_protocol_error(self) -> None:
        # An HTML error page decoded as JSON, or a bare string. Tolerating this
        # would report "0 cameras" for what is really a wrong base_url.
        for payload in ("cam-1,cam-2", 42, None, True, 3.5):
            with self.subTest(payload=payload):
                with self.assertRaises(FederationError):
                    sentinel_grid.parse_catalogue(payload)

    def test_non_dict_entries_are_skipped_not_fatal(self) -> None:
        cameras = sentinel_grid.parse_catalogue(
            [{"id": "cam-1"}, "cam-2", None, 7, ["cam-3"], {"id": "cam-4"}]
        )
        self.assertEqual([c.external_id for c in cameras], ["cam-1", "cam-4"])

    def test_entry_without_any_identifier_is_dropped(self) -> None:
        """No id means no stable key, and a synthesised one duplicates every sync.

        Inventing ``cam-<index>`` here would create a new camera row on every run,
        because the index shifts as the grid changes.
        """
        cameras = sentinel_grid.parse_catalogue(
            [{"lat": 23.0, "lon": 72.0, "codec": "h264"}, {"id": "", "name": ""}]
        )
        self.assertEqual(cameras, [])


# ---------------------------------------------------------------------------
# Field resolution
# ---------------------------------------------------------------------------


class FieldResolution(unittest.TestCase):
    """Alternate key spellings, coercion, and what must degrade to NULL."""

    def test_alternate_identifier_spellings(self) -> None:
        for key in ("id", "stream_id", "streamId", "camera_id", "cameraId", "path"):
            with self.subTest(key=key):
                cameras = sentinel_grid.parse_catalogue([{key: "cam-x"}])
                self.assertEqual(cameras[0].external_id, "cam-x")

    def test_name_serves_as_identifier_of_last_resort(self) -> None:
        # MediaMTX path names *are* the identifier, so this is not a guess.
        cameras = sentinel_grid.parse_catalogue([{"name": "junction_07"}])
        self.assertEqual(cameras[0].external_id, "junction_07")
        self.assertEqual(cameras[0].name, "junction_07")

    def test_explicit_id_beats_name(self) -> None:
        cameras = sentinel_grid.parse_catalogue([{"id": "cam-1", "name": "CG Road"}])
        self.assertEqual(cameras[0].external_id, "cam-1")
        self.assertEqual(cameras[0].name, "CG Road")

    def test_alternate_coordinate_spellings(self) -> None:
        for lat_key, lon_key in (
            ("lat", "lon"),
            ("latitude", "longitude"),
            ("lat", "lng"),
            ("lat", "long"),
            ("Latitude", "Longitude"),
            ("y", "x"),
        ):
            with self.subTest(keys=(lat_key, lon_key)):
                cameras = sentinel_grid.parse_catalogue(
                    [{"id": "c", lat_key: 23.0225, lon_key: 72.5714}]
                )
                self.assertAlmostEqual(cameras[0].latitude, 23.0225)
                self.assertAlmostEqual(cameras[0].longitude, 72.5714)

    def test_string_coordinates_are_coerced(self) -> None:
        cameras = sentinel_grid.parse_catalogue([{"id": "c", "lat": "23.02", "lon": "72.57"}])
        self.assertAlmostEqual(cameras[0].latitude, 23.02)
        self.assertTrue(cameras[0].has_location)

    def test_camera_with_no_coordinates_is_kept(self) -> None:
        """An unlocatable camera is still an asset the state paid for.

        Dropping it in the parser would hide it from the very reconciliation
        report that should be flagging it. ``app.camera.location`` is NOT NULL, so
        the sync layer rejects the row — but it rejects it *visibly*, with a
        reason, into ``import_batch.report``.
        """
        cameras = sentinel_grid.parse_catalogue([{"id": "cam-nogeo", "name": "Unknown site"}])
        self.assertEqual(len(cameras), 1)
        self.assertIsNone(cameras[0].latitude)
        self.assertIsNone(cameras[0].longitude)
        self.assertFalse(cameras[0].has_location)

    def test_partial_coordinates_are_not_half_a_location(self) -> None:
        cameras = sentinel_grid.parse_catalogue([{"id": "c", "lat": 23.02}])
        self.assertIsNotNone(cameras[0].latitude)
        self.assertIsNone(cameras[0].longitude)
        self.assertFalse(cameras[0].has_location)

    def test_unusable_coordinates_degrade_to_none(self) -> None:
        # NaN and infinity survive float() and then poison a PostGIS geography
        # column; JSON true would silently become 1.0, i.e. a point off Africa.
        for value in ("null", "N/A", "", "abc", float("nan"), float("inf"), True, [23.0]):
            with self.subTest(value=value):
                cameras = sentinel_grid.parse_catalogue([{"id": "c", "lat": value, "lon": value}])
                self.assertIsNone(cameras[0].latitude)
                self.assertIsNone(cameras[0].longitude)

    def test_codec_spellings_map_onto_the_column_vocabulary(self) -> None:
        for given, expected in (
            ("H.264", "h264"),
            ("h264", "h264"),
            ("AVC", "h264"),
            ("HEVC", "h265"),
            ("H.265", "h265"),
            ("MJPEG", "mjpeg"),
            ("AV1", "av1"),
            ("MPEG-4", "mpeg4"),
        ):
            with self.subTest(codec=given):
                cameras = sentinel_grid.parse_catalogue([{"id": "c", "codec": given}])
                self.assertEqual(cameras[0].codec, expected)
                self.assertIn(cameras[0].codec, {"h264", "h265", "mjpeg", "av1", "mpeg4"})

    def test_unknown_codec_becomes_null_not_a_guess(self) -> None:
        # 002_camera.sql CHECK-constrains this column; a guess would either
        # violate it or record a fabrication that later heuristics trust.
        for given in ("vp9", "", "  ", None, "h.266", 12):
            with self.subTest(codec=given):
                cameras = sentinel_grid.parse_catalogue([{"id": "c", "codec": given}])
                self.assertIsNone(cameras[0].codec)

    def test_camera_type_never_over_claims_capability(self) -> None:
        cameras = sentinel_grid.parse_catalogue([{"id": "c", "type": "quantum-camera"}])
        self.assertEqual(cameras[0].camera_type, "fixed")
        self.assertIn(cameras[0].camera_type, base.CAMERA_TYPES)

    def test_camera_type_aliases(self) -> None:
        for given, expected in (
            ("PTZ", "ptz"),
            ("speed dome", "ptz"),
            ("LPR", "anpr"),
            ("fisheye", "panoramic"),
            ("body-worn", "body_worn"),
            ("body_worn", "body_worn"),
            ("dashcam", "mobile"),
        ):
            with self.subTest(type=given):
                cameras = sentinel_grid.parse_catalogue([{"id": "c", "type": given}])
                self.assertEqual(cameras[0].camera_type, expected)

    def test_resolution_as_a_single_string(self) -> None:
        for given in ("1920x1080", "1920X1080", "1920*1080", " 1920 x 1080 "):
            with self.subTest(resolution=given):
                cameras = sentinel_grid.parse_catalogue([{"id": "c", "resolution": given}])
                self.assertEqual((cameras[0].resolution_w, cameras[0].resolution_h), (1920, 1080))

    def test_split_resolution_fields_win_over_a_summary_string(self) -> None:
        cameras = sentinel_grid.parse_catalogue(
            [{"id": "c", "width": 3840, "height": 2160, "resolution": "1920x1080"}]
        )
        self.assertEqual((cameras[0].resolution_w, cameras[0].resolution_h), (3840, 2160))

    def test_unparseable_resolution_is_null_not_zero(self) -> None:
        for given in ("HD", "1080p", "", "x", "1920x"):
            with self.subTest(resolution=given):
                cameras = sentinel_grid.parse_catalogue([{"id": "c", "resolution": given}])
                self.assertIsNone(cameras[0].resolution_h)

    def test_unrecognised_keys_are_preserved_in_raw(self) -> None:
        """``raw`` is what makes tightening this parser possible later.

        The first successful sync's raw blobs are the specification for the strict
        parser that should replace the tolerant one.
        """
        cameras = sentinel_grid.parse_catalogue(
            [
                {
                    "id": "cam-1",
                    "lat": 23.0,
                    "lon": 72.0,
                    "bitrate_kbps": 4096,
                    "vendor_extension": {"ai": ["anpr"]},
                    "ready": True,
                }
            ]
        )
        self.assertEqual(cameras[0].raw["bitrate_kbps"], 4096)
        self.assertEqual(cameras[0].raw["vendor_extension"], {"ai": ["anpr"]})
        self.assertTrue(cameras[0].raw["ready"])

    def test_consumed_keys_do_not_appear_twice(self) -> None:
        cameras = sentinel_grid.parse_catalogue(
            [{"stream_id": "cam-1", "lat": 23.0, "lon": 72.0, "codec": "h264"}]
        )
        for key in ("stream_id", "lat", "lon", "codec"):
            self.assertNotIn(key, cameras[0].raw)

    def test_non_url_source_is_not_treated_as_a_url(self) -> None:
        """MediaMTX's ``source`` is often a descriptor, not an address.

        ``"publisher"`` stored in ``camera.rtsp_url`` gives the viewer a string it
        will dial, producing a failure that looks like a dead camera rather than a
        bad import.
        """
        for value in ("publisher", "rpiCamera", "redirect", "", None, 42):
            with self.subTest(source=value):
                cameras = sentinel_grid.parse_catalogue([{"id": "c", "source": value}])
                self.assertIsNone(cameras[0].rtsp_url)

    def test_published_urls_are_carried_through_verbatim(self) -> None:
        cameras = sentinel_grid.parse_catalogue(
            [
                {
                    "id": "cam-1",
                    "rtsp_url": "rtsp://grid.example:8554/stream/cam-1",
                    "hls": "http://grid.example/live/stream/cam-1/index.m3u8",
                    "whep": "http://grid.example:8889/stream/cam-1/whep",
                }
            ]
        )
        camera = cameras[0]
        self.assertEqual(camera.rtsp_url, "rtsp://grid.example:8554/stream/cam-1")
        self.assertEqual(camera.hls_url, "http://grid.example/live/stream/cam-1/index.m3u8")
        self.assertEqual(camera.whep_url, "http://grid.example:8889/stream/cam-1/whep")


# ---------------------------------------------------------------------------
# Sandbox URL resolution
# ---------------------------------------------------------------------------


class SandboxUrlResolution(unittest.TestCase):
    """The catalogue is the contract; the pattern is only a fallback."""

    def test_catalogue_url_beats_the_pattern(self) -> None:
        driver = grid()
        # Pre-seeding the cache stands in for a completed list_cameras() and keeps
        # this test off the network.
        driver._cache = {
            "cam-1": ForeignCamera(
                external_id="cam-1",
                name="cam-1",
                latitude=None,
                longitude=None,
                rtsp_url="rtsp://elsewhere.example:9999/odd/path",
            )
        }
        descriptor = run(driver.stream_url("cam-1", protocol="rtsp"))
        self.assertEqual(descriptor.url, "rtsp://elsewhere.example:9999/odd/path")
        self.assertIn("catalogue", descriptor.detail)

    def test_fallback_patterns_match_the_documented_shape(self) -> None:
        driver = grid()
        driver._cache = {"other": ForeignCamera("other", "other", None, None)}
        cases = {
            "rtsp": "rtsp://sandbox.example:8554/stream/cam-1",
            "whep": "http://sandbox.example:8889/stream/cam-1/whep",
            "hls": "http://sandbox.example:8080/live/stream/cam-1/index.m3u8",
        }
        for protocol, expected in cases.items():
            with self.subTest(protocol=protocol):
                descriptor = run(driver.stream_url("cam-1", protocol=protocol))
                self.assertEqual(descriptor.url, expected)
                self.assertEqual(descriptor.protocol, protocol)
                self.assertIn("fallback", descriptor.detail)

    def test_protocol_absent_from_the_catalogue_falls_back(self) -> None:
        driver = grid()
        driver._cache = {
            "cam-1": ForeignCamera(
                external_id="cam-1", name="cam-1", latitude=None, longitude=None,
                rtsp_url="rtsp://published.example:8554/stream/cam-1",
            )
        }
        descriptor = run(driver.stream_url("cam-1", protocol="whep"))
        self.assertEqual(descriptor.url, "http://sandbox.example:8889/stream/cam-1/whep")

    def test_stream_id_is_percent_encoded(self) -> None:
        # A path name with a space produces a different URL if pasted in raw.
        driver = grid()
        driver._cache = {"other": ForeignCamera("other", "other", None, None)}
        descriptor = run(driver.stream_url("junction 07/main", protocol="rtsp"))
        self.assertEqual(descriptor.url, "rtsp://sandbox.example:8554/stream/junction%2007%2Fmain")

    def test_patterns_are_configurable(self) -> None:
        # Demo-day port changes must be an env edit, not a code change.
        driver = grid(rtsp_pattern="rtsp://{host}:1935/live/{id}")
        driver._cache = {"other": ForeignCamera("other", "other", None, None)}
        descriptor = run(driver.stream_url("cam-1", protocol="rtsp"))
        self.assertEqual(descriptor.url, "rtsp://sandbox.example:1935/live/cam-1")

    def test_base_url_without_a_scheme_is_refused(self) -> None:
        # Guessing http:// would be guessing whether credentials cross the network
        # in the clear.
        with self.assertRaises(FederationError):
            sentinel_grid.SentinelGridDriver({"base_url": "sandbox.example:8080"})

    def test_base_url_is_required(self) -> None:
        with self.assertRaises(FederationError) as caught:
            sentinel_grid.SentinelGridDriver({})
        self.assertIn("base_url", str(caught.exception))

    def test_trailing_slash_does_not_double_up(self) -> None:
        driver = sentinel_grid.SentinelGridDriver({"base_url": "http://sandbox.example:8080/"})
        self.assertEqual(driver.base_url, "http://sandbox.example:8080")
        self.assertEqual(driver.origin, "http://sandbox.example:8080")
        self.assertEqual(driver.host, "sandbox.example")

    def test_recording_is_refused_rather_than_reported_empty(self) -> None:
        """``None`` would mean "the archive holds nothing for that window".

        The console would render that as a legitimately empty timeline. The grid
        has no archive at all, which is a different statement.
        """
        driver = grid()
        now = datetime.now(timezone.utc)
        with self.assertRaises(NotSupported):
            run(driver.recording_url("cam-1", now - timedelta(hours=1), now))

    def test_ptz_is_refused_not_silently_ignored(self) -> None:
        driver = grid()
        with self.assertRaises(NotSupported):
            run(driver.ptz("cam-1", PtzCommand(action="pan_tilt", pan=0.5)))

    def test_close_is_idempotent_without_a_client(self) -> None:
        driver = grid()
        run(driver.close())
        run(driver.close())

    def test_parser_does_not_need_httpx(self) -> None:
        """The risky part of this module must be testable with nothing installed.

        ``httpx`` is imported inside the methods that build a client, so the
        module — and therefore ``parse_catalogue`` — imports on a bare
        interpreter. If someone moves that import to module scope, this fails.
        """
        self.assertNotIn("httpx", vars(sentinel_grid))
        self.assertNotIn("httpx", vars(onvif_rtsp))


# ---------------------------------------------------------------------------
# Driver lookup
# ---------------------------------------------------------------------------


class DriverLookup(unittest.TestCase):
    def test_all_four_platforms_are_available(self) -> None:
        self.assertEqual(
            registry.available_platforms(),
            (
                "genetec_security_center",
                "milestone_xprotect",
                "onvif_rtsp",
                "sentinel_grid",
            ),
        )

    def test_each_key_matches_its_class_platform(self) -> None:
        # Drift here means a camera row's vms_platform resolves to the wrong
        # driver, or to nothing.
        for name in registry.available_platforms():
            with self.subTest(platform=name):
                driver = registry.get_driver(name, _config_for(name))
                self.assertEqual(driver.platform, name)

    def test_unknown_platform_names_the_alternatives(self) -> None:
        with self.assertRaises(FederationError) as caught:
            registry.get_driver("hikvision_ivms", {})
        message = str(caught.exception)
        self.assertIn("hikvision_ivms", message)
        for name in registry.available_platforms():
            self.assertIn(name, message)

    def test_case_and_whitespace_are_normalised(self) -> None:
        # vms_platform is free text arriving from spreadsheets.
        for name in (" sentinel_grid", "SENTINEL_GRID", "Sentinel_Grid ", "\tsentinel_grid\n"):
            with self.subTest(name=name):
                driver = registry.get_driver(name, SANDBOX)
                self.assertIsInstance(driver, sentinel_grid.SentinelGridDriver)

    def test_blank_and_non_string_platforms_are_refused(self) -> None:
        for name in ("", "   ", None, 7, ["sentinel_grid"]):
            with self.subTest(name=name):
                with self.assertRaises(FederationError):
                    registry.get_driver(name, {})

    def test_a_module_path_is_not_a_platform(self) -> None:
        """The reason the lookup table is closed.

        ``vms_platform`` is attacker-influenced in the worst case — bulk CSV
        import is an ordinary user privilege. Dynamic import by that string would
        turn "can upload a camera list" into "can import any module in the
        process".
        """
        for name in (
            "app.federation.registry",
            "os",
            "app.core.crypto",
            "../../etc/passwd",
            "app.federation.sentinel_grid.SentinelGridDriver",
        ):
            with self.subTest(name=name):
                with self.assertRaises(FederationError):
                    registry.get_driver(name, SANDBOX)

    def test_register_driver_rejects_a_duplicate_platform(self) -> None:
        class Impostor(sentinel_grid.SentinelGridDriver):
            pass  # inherits platform = "sentinel_grid"

        with self.assertRaises(FederationError):
            registry.register_driver(Impostor)

    def test_register_driver_rejects_a_driver_with_no_platform(self) -> None:
        class Nameless(VmsDriver):
            async def list_cameras(self) -> list[ForeignCamera]:
                return []

            async def stream_url(self, vms_camera_id, *, protocol):
                raise NotSupported("test")

            async def recording_url(self, vms_camera_id, start, end):
                return None

            async def health(self, vms_camera_id):
                return DriverHealth(is_live=True, probe=base.PROBE_TCP)

            async def close(self) -> None:
                return None

        with self.assertRaises(FederationError):
            registry.register_driver(Nameless)


def _config_for(platform: str) -> dict[str, Any]:
    """Minimum viable config per platform, for construction-only tests."""
    if platform == "sentinel_grid":
        return dict(SANDBOX)
    if platform == "onvif_rtsp":
        return {"rtsp_url": "rtsp://10.0.0.5:554/Streaming/Channels/101"}
    return {}


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class StubsRefuseHonestly(unittest.TestCase):
    """A stub that lies is worse than a stub that refuses."""

    drivers = (milestone.MilestoneDriver, genetec.GenetecDriver)

    def test_construction_succeeds_so_failure_lands_at_the_point_of_use(self) -> None:
        for cls in self.drivers:
            with self.subTest(driver=cls.__name__):
                self.assertIsInstance(cls({}), VmsDriver)

    def test_every_contract_method_refuses(self) -> None:
        now = datetime.now(timezone.utc)
        for cls in self.drivers:
            driver = cls({})
            calls = {
                "list_cameras": driver.list_cameras(),
                "stream_url": driver.stream_url("x", protocol="rtsp"),
                "recording_url": driver.recording_url("x", now, now),
                "health": driver.health("x"),
                "ptz": driver.ptz("x", PtzCommand(action="stop")),
            }
            for method, coro in calls.items():
                with self.subTest(driver=cls.__name__, method=method):
                    with self.assertRaises(NotSupported) as caught:
                        run(coro)
                    # The message must say what to go and obtain, not just "no".
                    self.assertIn("credentials", str(caught.exception).lower())

    def test_health_does_not_fabricate_a_down_row(self) -> None:
        """``is_live=False`` would be a lie with a database row behind it.

        It would enter ``app.camera_health_check`` as evidence that a camera was
        probed and found down, and then be counted in uptime reporting. Nothing
        was probed.
        """
        for cls in self.drivers:
            with self.subTest(driver=cls.__name__):
                with self.assertRaises(NotSupported):
                    run(cls({}).health("x"))

    def test_close_is_safe(self) -> None:
        for cls in self.drivers:
            with self.subTest(driver=cls.__name__):
                run(cls({}).close())


# ---------------------------------------------------------------------------
# RTSP status line
# ---------------------------------------------------------------------------


class RtspStatusLineParsing(unittest.TestCase):
    def test_well_formed_lines(self) -> None:
        cases = {
            "RTSP/1.0 200 OK\r\n": (200, "OK"),
            "RTSP/1.0 401 Unauthorized\r\n": (401, "Unauthorized"),
            "RTSP/1.0 454 Session Not Found\r\n": (454, "Session Not Found"),
            "RTSP/1.0 501 Not Implemented": (501, "Not Implemented"),
            "RTSP/2.0 200 OK\r\n": (200, "OK"),
            "RTSP/1.0 200": (200, ""),  # reason phrase is optional in RFC 2326
            "RTSP/1.0   200   OK  \r\n": (200, "OK"),
            "rtsp/1.0 200 OK\r\n": (200, "OK"),  # seen from at least one NVR
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                self.assertEqual(onvif_rtsp.parse_rtsp_status_line(line), expected)

    def test_bytes_input(self) -> None:
        self.assertEqual(
            onvif_rtsp.parse_rtsp_status_line(b"RTSP/1.0 200 OK\r\n"), (200, "OK")
        )

    def test_an_http_server_on_554_is_not_a_camera(self) -> None:
        """The most informative thing this probe can report.

        A web interface, a captive portal or a port-forward pointing at the wrong
        host all answer on 554 and all look healthy to a tolerant parser.
        """
        with self.assertRaises(FederationError) as caught:
            onvif_rtsp.parse_rtsp_status_line("HTTP/1.1 200 OK\r\n")
        self.assertIn("listening", str(caught.exception))

    def test_malformed_lines_are_rejected(self) -> None:
        for line in (
            "",
            "   \r\n",
            b"",
            "garbage",
            "HTTP/1.1 404 Not Found",
            "RTSP/1.0",  # no status code at all
            "RTSP/1.0 abc OK",
            "RTSP/1.0 99 Too Small",
            "RTSP/1.0 600 Too Big",
            "RTSP/1.0 -200 Negative",
            "<html><body>Login</body></html>",
            "SIP/2.0 200 OK",
        ):
            with self.subTest(line=line):
                with self.assertRaises(FederationError):
                    onvif_rtsp.parse_rtsp_status_line(line)

    def test_non_ascii_status_line_is_rejected(self) -> None:
        with self.assertRaises(FederationError):
            onvif_rtsp.parse_rtsp_status_line(b"RTSP/1.0 200 \xff\xfe OK")


# ---------------------------------------------------------------------------
# Direct-connect driver
# ---------------------------------------------------------------------------


class _FakeWriter:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeReader:
    def __init__(self, line: bytes) -> None:
        self.line = line

    async def readline(self) -> bytes:
        return self.line


def _fake_connection(line: bytes) -> tuple[Any, _FakeWriter]:
    """A substitute for asyncio.open_connection that replays one status line."""
    writer = _FakeWriter()

    async def opener(host: str, port: int) -> tuple[Any, _FakeWriter]:
        return _FakeReader(line), writer

    return opener, writer


def _raising_opener(exc: BaseException):
    async def opener(host: str, port: int):
        raise exc

    return opener


RTSP_CONFIG = {"rtsp_url": "rtsp://10.20.30.40:554/Streaming/Channels/101"}


class DirectConnectTargets(unittest.TestCase):
    def test_userinfo_is_stripped_from_the_request_uri(self) -> None:
        """Camera passwords must not end up in the camera's own access log."""
        host, port, uri = onvif_rtsp._split_rtsp_target(
            "rtsp://admin:hunter2@10.0.0.5:554/live"
        )
        self.assertEqual((host, port), ("10.0.0.5", 554))
        self.assertEqual(uri, "rtsp://10.0.0.5:554/live")
        self.assertNotIn("hunter2", uri)
        self.assertNotIn("admin", uri)

    def test_default_port_is_filled_in(self) -> None:
        host, port, uri = onvif_rtsp._split_rtsp_target("rtsp://10.0.0.5/live")
        self.assertEqual(port, 554)
        self.assertEqual(uri, "rtsp://10.0.0.5:554/live")

    def test_query_string_is_preserved(self) -> None:
        # ?channel=1&subtype=0 is load-bearing on Dahua and Hikvision URLs.
        _, _, uri = onvif_rtsp._split_rtsp_target("rtsp://10.0.0.5/cam?channel=1&subtype=0")
        self.assertEqual(uri, "rtsp://10.0.0.5:554/cam?channel=1&subtype=0")

    def test_non_rtsp_scheme_is_refused(self) -> None:
        for url in ("http://10.0.0.5/live", "rtmp://10.0.0.5/live", "10.0.0.5/live"):
            with self.subTest(url=url):
                with self.assertRaises(FederationError):
                    onvif_rtsp._split_rtsp_target(url)

    def test_url_without_a_host_is_refused(self) -> None:
        with self.assertRaises(FederationError):
            onvif_rtsp._split_rtsp_target("rtsp:///live")


class DirectConnectDriver(unittest.TestCase):
    def test_construction_requires_an_address(self) -> None:
        with self.assertRaises(FederationError):
            onvif_rtsp.OnvifRtspDriver({})

    def test_onvif_only_configuration_is_valid(self) -> None:
        driver = onvif_rtsp.OnvifRtspDriver(
            {"onvif_url": "http://10.0.0.5/onvif/device_service"}
        )
        self.assertIsNone(driver.rtsp_url)

    def test_stream_url_appends_nothing(self) -> None:
        """Transport options belong to the consumer, not to the stored URL.

        Three different clients read this value; baking one client's flags into it
        breaks the other two.
        """
        url = "rtsp://10.20.30.40:554/cam/realmonitor?channel=1&subtype=0"
        driver = onvif_rtsp.OnvifRtspDriver({"rtsp_url": url})
        descriptor = run(driver.stream_url("ignored", protocol="rtsp"))
        self.assertEqual(descriptor.url, url)
        self.assertNotIn("rtsp_transport", descriptor.url)
        self.assertIsNone(descriptor.expires_at)
        self.assertFalse(descriptor.is_expired)
        # The requirement is communicated, not injected.
        self.assertIn("tcp", descriptor.detail)

    def test_credentials_are_assumed_required_by_default(self) -> None:
        driver = onvif_rtsp.OnvifRtspDriver(dict(RTSP_CONFIG))
        self.assertTrue(run(driver.stream_url("x", protocol="rtsp")).requires_credentials)

    def test_unconfigured_protocol_is_not_supported(self) -> None:
        driver = onvif_rtsp.OnvifRtspDriver(dict(RTSP_CONFIG))
        for protocol in ("hls", "whep"):
            with self.subTest(protocol=protocol):
                with self.assertRaises(NotSupported):
                    run(driver.stream_url("x", protocol=protocol))

    def test_list_cameras_refuses_without_inventory_metadata(self) -> None:
        """Returning [] would read as "this platform has no cameras".

        The sync layer would then be entitled to retire perfectly healthy rows.
        """
        driver = onvif_rtsp.OnvifRtspDriver(dict(RTSP_CONFIG))
        with self.assertRaises(NotSupported):
            run(driver.list_cameras())

    def test_list_cameras_returns_exactly_one(self) -> None:
        driver = onvif_rtsp.OnvifRtspDriver(
            {
                **RTSP_CONFIG,
                "onvif_url": "http://10.20.30.40/onvif/device_service",
                "camera": {
                    "external_id": "AHM-NAV-0142",
                    "name": "Navrangpura Circle",
                    "latitude": 23.0367,
                    "longitude": 72.5613,
                    "codec": "H.265",
                    "camera_type": "PTZ",
                    "make": "Hikvision",
                },
            }
        )
        cameras = run(driver.list_cameras())
        self.assertEqual(len(cameras), 1)
        camera = cameras[0]
        self.assertEqual(camera.external_id, "AHM-NAV-0142")
        self.assertEqual(camera.codec, "h265")
        self.assertEqual(camera.camera_type, "ptz")
        self.assertEqual(camera.rtsp_url, RTSP_CONFIG["rtsp_url"])
        self.assertEqual(camera.onvif_url, "http://10.20.30.40/onvif/device_service")
        self.assertEqual(camera.raw["make"], "Hikvision")

    def test_camera_metadata_without_an_id_is_a_config_error(self) -> None:
        driver = onvif_rtsp.OnvifRtspDriver({**RTSP_CONFIG, "camera": {"name": "no id"}})
        with self.assertRaises(FederationError):
            run(driver.list_cameras())

    def test_recording_is_refused(self) -> None:
        driver = onvif_rtsp.OnvifRtspDriver(dict(RTSP_CONFIG))
        now = datetime.now(timezone.utc)
        with self.assertRaises(NotSupported):
            run(driver.recording_url("x", now - timedelta(minutes=5), now))

    def test_ptz_is_refused(self) -> None:
        driver = onvif_rtsp.OnvifRtspDriver(dict(RTSP_CONFIG))
        with self.assertRaises(NotSupported):
            run(driver.ptz("x", PtzCommand(action="home")))

    def test_onvif_envelope_is_a_real_soap_12_request(self) -> None:
        envelope = onvif_rtsp.ONVIF_GET_SYSTEM_DATE_AND_TIME
        self.assertIn("http://www.w3.org/2003/05/soap-envelope", envelope)
        self.assertIn("http://www.onvif.org/ver10/device/wsdl", envelope)
        self.assertIn("GetSystemDateAndTime", envelope)
        # No credential material: this call is the unauthenticated probe.
        self.assertNotIn("Security", envelope)
        self.assertNotIn("UsernameToken", envelope)


class HealthProbeMapping(unittest.TestCase):
    """The probe must never establish a media session, and must name the fault."""

    def _probe(self, opener: Any, config: dict[str, Any] | None = None) -> DriverHealth:
        driver = onvif_rtsp.OnvifRtspDriver(config or dict(RTSP_CONFIG))
        with mock.patch("asyncio.open_connection", new=opener):
            return run(driver.health("ignored"))

    def test_probe_sends_options_and_stops_there(self) -> None:
        """The architectural invariant, asserted on the bytes.

        ``SETUP`` is where the server allocates a session and reserves transport.
        Across 80,000 cameras — much of it on hardware limited to a handful of
        concurrent clients — a probe that reached ``SETUP`` would be a
        self-inflicted denial of service.
        """
        opener, writer = _fake_connection(b"RTSP/1.0 200 OK\r\n")
        health = self._probe(opener)
        sent = bytes(writer.buffer).decode("ascii")

        self.assertTrue(sent.startswith("OPTIONS rtsp://10.20.30.40:554/"))
        for forbidden in ("SETUP", "PLAY", "DESCRIBE", "Transport:"):
            self.assertNotIn(forbidden, sent)
        self.assertIn("CSeq: 1", sent)
        self.assertTrue(sent.endswith("\r\n\r\n"))
        self.assertTrue(writer.closed)
        self.assertTrue(health.is_live)
        self.assertEqual(health.probe, "rtsp_options")
        self.assertIsNotNone(health.latency_ms)
        self.assertGreaterEqual(health.latency_ms, 0)

    def test_credentials_are_never_written_to_the_wire(self) -> None:
        opener, writer = _fake_connection(b"RTSP/1.0 200 OK\r\n")
        self._probe(opener, {"rtsp_url": "rtsp://admin:hunter2@10.20.30.40:554/live"})
        sent = bytes(writer.buffer).decode("ascii")
        self.assertNotIn("hunter2", sent)
        self.assertNotIn("admin", sent)

    def test_401_means_alive_and_needing_a_credential(self) -> None:
        """Recording a 401 as "down" sends an engineer to a working site."""
        opener, _ = _fake_connection(b"RTSP/1.0 401 Unauthorized\r\n")
        health = self._probe(opener)
        self.assertTrue(health.is_live)
        self.assertEqual(health.error_code, "auth")

    def test_method_not_allowed_still_proves_life(self) -> None:
        # A server that answers at all is running, whatever it thinks of OPTIONS.
        opener, _ = _fake_connection(b"RTSP/1.0 405 Method Not Allowed\r\n")
        self.assertTrue(self._probe(opener).is_live)

    def test_server_error_is_not_alive(self) -> None:
        opener, _ = _fake_connection(b"RTSP/1.0 500 Internal Server Error\r\n")
        health = self._probe(opener)
        self.assertFalse(health.is_live)
        self.assertEqual(health.error_code, "upstream")

    def test_a_non_rtsp_reply_is_a_protocol_fault(self) -> None:
        opener, _ = _fake_connection(b"HTTP/1.1 200 OK\r\n")
        health = self._probe(opener)
        self.assertFalse(health.is_live)
        self.assertEqual(health.error_code, "protocol")

    def test_silent_close_is_a_protocol_fault(self) -> None:
        opener, _ = _fake_connection(b"")
        self.assertEqual(self._probe(opener).error_code, "protocol")

    def test_failure_modes_map_to_distinct_codes(self) -> None:
        """Timeout, refused, DNS and unreachable send an engineer to four places.

        Note the ordering hazard this covers: on Python 3.11+
        ``asyncio.TimeoutError`` *is* the builtin ``TimeoutError``, a subclass of
        ``OSError``. An ``except OSError`` placed first would report every timeout
        as "unreachable" — congestion misdiagnosed as a routing fault.
        """
        cases = (
            (asyncio.TimeoutError(), "timeout"),
            (ConnectionRefusedError(111, "refused"), "refused"),
            (socket.gaierror(-2, "Name or service not known"), "dns"),
            (OSError(113, "No route to host"), "unreachable"),
            (ConnectionResetError(104, "reset by peer"), "unreachable"),
        )
        for exc, expected in cases:
            with self.subTest(exception=type(exc).__name__):
                health = self._probe(_raising_opener(exc))
                self.assertFalse(health.is_live)
                self.assertEqual(health.error_code, expected)
                self.assertEqual(health.probe, "rtsp_options")

    def test_builtin_timeout_error_is_also_a_timeout(self) -> None:
        # asyncio.wait_for raises the builtin on 3.11+; both spellings must map
        # to 'timeout' rather than falling through to the OSError branch.
        health = self._probe(_raising_opener(TimeoutError()))
        self.assertEqual(health.error_code, "timeout")

    def test_malformed_configured_url_is_a_row_not_an_exception(self) -> None:
        """One typo in the inventory must not stop the whole sweep."""
        driver = onvif_rtsp.OnvifRtspDriver({"rtsp_url": "http://10.0.0.5/not-rtsp"})
        health = run(driver.health("x"))
        self.assertFalse(health.is_live)
        self.assertEqual(health.error_code, "protocol")

    def test_health_result_is_insertable_as_is(self) -> None:
        # Fields and vocabularies mirror app.camera_health_check so the worker is
        # a straight INSERT.
        opener, _ = _fake_connection(b"RTSP/1.0 200 OK\r\n")
        health = self._probe(opener)
        self.assertIn(health.probe, base.PROBES)
        self.assertIsInstance(health.is_live, bool)
        self.assertIsInstance(health.checked_at, datetime)
        self.assertIsNotNone(health.checked_at.tzinfo)


# ---------------------------------------------------------------------------
# Contract invariants
# ---------------------------------------------------------------------------


class ContractInvariants(unittest.TestCase):
    def test_the_base_class_cannot_be_instantiated(self) -> None:
        with self.assertRaises(TypeError):
            VmsDriver({})  # type: ignore[abstract]

    def test_foreign_camera_is_immutable(self) -> None:
        camera = ForeignCamera("cam-1", "Cam 1", 23.0, 72.0)
        with self.assertRaises(Exception):
            camera.latitude = 0.0  # type: ignore[misc]

    def test_stream_descriptor_expiry(self) -> None:
        now = datetime.now(timezone.utc)
        self.assertFalse(
            StreamDescriptor("rtsp://x/y", "rtsp", expires_at=now + timedelta(minutes=5)).is_expired
        )
        self.assertTrue(
            StreamDescriptor("rtsp://x/y", "rtsp", expires_at=now - timedelta(seconds=1)).is_expired
        )
        # No stated expiry is not the same as "never expires", but it is all we
        # can assert, so is_expired stays False.
        self.assertFalse(StreamDescriptor("rtsp://x/y", "rtsp").is_expired)

    def test_health_rejects_a_probe_the_database_would_reject(self) -> None:
        # Failing here names the driver; failing at INSERT names a constraint.
        for probe in ("rtsp_play", "ping", "", "TCP"):
            with self.subTest(probe=probe):
                with self.assertRaises(FederationError):
                    DriverHealth(is_live=True, probe=probe)

    def test_health_rejects_negative_latency(self) -> None:
        with self.assertRaises(FederationError):
            DriverHealth(is_live=True, probe=base.PROBE_TCP, latency_ms=-1)

    def test_ptz_velocities_are_bounded(self) -> None:
        for kwargs in (
            {"pan": 1.5},
            {"tilt": -1.01},
            {"zoom": 42.0},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(FederationError):
                    PtzCommand(action="pan_tilt", **kwargs)
        PtzCommand(action="pan_tilt", pan=1.0, tilt=-1.0, zoom=0.0)

    def test_preset_action_requires_a_token(self) -> None:
        with self.assertRaises(FederationError):
            PtzCommand(action="preset")
        PtzCommand(action="preset", preset="gate-3")

    def test_duration_must_be_positive_when_given(self) -> None:
        for duration in (0, -500):
            with self.subTest(duration=duration):
                with self.assertRaises(FederationError):
                    PtzCommand(action="pan_tilt", pan=0.2, duration_ms=duration)

    def test_timeout_must_be_positive(self) -> None:
        for timeout in (0, -1):
            with self.subTest(timeout=timeout):
                with self.assertRaises(FederationError):
                    sentinel_grid.SentinelGridDriver({**SANDBOX, "timeout": timeout})

    def test_missing_required_config_names_the_key_and_the_platform(self) -> None:
        with self.assertRaises(FederationError) as caught:
            sentinel_grid.SentinelGridDriver({"base_url": ""})
        message = str(caught.exception)
        self.assertIn("base_url", message)
        self.assertIn("sentinel_grid", message)

    def test_repr_does_not_leak_config_values(self) -> None:
        """Config holds decrypted camera passwords. Reprs reach logs."""
        driver = onvif_rtsp.OnvifRtspDriver(
            {"rtsp_url": "rtsp://admin:hunter2@10.0.0.5/live", "password": "hunter2"}
        )
        self.assertNotIn("hunter2", repr(driver))

    def test_error_hierarchy(self) -> None:
        # Callers that only care that federation failed catch FederationError.
        for cls in (NotSupported, base.UpstreamUnavailable, base.AuthenticationFailed):
            with self.subTest(error=cls.__name__):
                self.assertTrue(issubclass(cls, FederationError))

    def test_vocabularies_match_the_migration(self) -> None:
        # Hardcoded from db/migrations/002_camera.sql on purpose: if someone
        # changes one side, this fails and forces them to look at the other.
        self.assertEqual(
            base.PROBES,
            frozenset({"tcp", "rtsp_options", "rtsp_describe", "onvif", "http"}),
        )
        self.assertEqual(
            base.CAMERA_TYPES,
            frozenset(
                {
                    "fixed", "ptz", "dome", "bullet", "anpr",
                    "thermal", "panoramic", "body_worn", "mobile",
                }
            ),
        )


if __name__ == "__main__":
    unittest.main()
