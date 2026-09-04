"""Adapter for the hackathon's Sentinel camera-grid sandbox.

The sandbox publishes a catalogue at ``GET {base_url}/api/ingest`` and serves the
streams themselves from a MediaMTX-shaped set of ports. **The catalogue is the
contract; the URL pattern is not.** Anything the catalogue hands us is used
verbatim; the patterns below are a fallback for entries that carry an id and
nothing else, and they are configurable so a changed port on demo day is an
environment-variable edit rather than a code change.

Consume-only, in both directions that matter:

* We never POST, PUT or DELETE against the sandbox. It is shared infrastructure
  for every team in the competition, and publishing into it — even by accident,
  even a health check that registers a source — would corrupt someone else's
  test run. The only verb this module uses is GET.
* We never pull media. ``list_cameras`` and ``health`` touch the JSON catalogue
  only; the returned URLs are handed to the browser or to an edge worker.

**On the defensive parser.** The exact JSON shape of ``/api/ingest`` has not been
verified against a live sandbox — see the network-reachability caveat in the
project notes. So the parser accepts a top-level list *or* a dict wrapping the
list under any of several plausible keys, and resolves each field by trying
several plausible key spellings. This tolerance is deliberate and temporary: it
buys the ability to demo against a sandbox whose shape we first observe minutes
before we need it. **It should be tightened to the observed shape as soon as
there is one**, because a parser this permissive will also happily accept a
payload that has silently changed meaning. Until then, every key we did not
consume is preserved in ``ForeignCamera.raw``, which is what makes the tightening
possible: the raw blobs from the first successful sync are the specification.
"""
from __future__ import annotations

import time
import urllib.parse
from datetime import datetime
from typing import Any, ClassVar

from .base import (
    ERR_NOT_FOUND,
    ERR_PROTOCOL,
    ERR_TIMEOUT,
    ERR_UPSTREAM,
    PROBE_HTTP,
    AuthenticationFailed,
    DriverHealth,
    FederationError,
    ForeignCamera,
    NotSupported,
    StreamDescriptor,
    StreamProtocol,
    UpstreamUnavailable,
    VmsDriver,
    normalise_camera_type,
    normalise_codec,
)

# The documented fallback patterns. `{host}` is the sandbox host without a port,
# `{id}` the stream id, `{origin}` the scheme://netloc of base_url.
DEFAULT_RTSP_PATTERN = "rtsp://{host}:8554/stream/{id}"
DEFAULT_WHEP_PATTERN = "http://{host}:8889/stream/{id}/whep"
# The documented HLS pattern carries no port, implying 80. We substitute the
# origin of base_url instead, so a sandbox reached on a non-default port still
# resolves — the catalogue and the HLS front door are the same service there.
# Verify against the live grid; if HLS is genuinely on port 80 regardless,
# override `hls_pattern` in config.
DEFAULT_HLS_PATTERN = "{origin}/live/stream/{id}/index.m3u8"

CATALOGUE_PATH = "/api/ingest"

# Key spellings tried in order, most specific first. Ordering matters: an entry
# carrying both `stream_id` and `name` should key off `stream_id`, and fall back
# to `name` only for entries where the name *is* the identifier — which is how
# MediaMTX-style path names behave.
_ID_KEYS = ("id", "stream_id", "streamId", "camera_id", "cameraId", "path", "name")
_NAME_KEYS = ("name", "title", "label", "camera_name", "display_name", "id")
_LAT_KEYS = ("lat", "latitude", "Latitude", "y")
_LON_KEYS = ("lon", "lng", "long", "longitude", "Longitude", "x")
_CODEC_KEYS = ("codec", "video_codec", "videoCodec", "encoding", "format")
_WIDTH_KEYS = ("width", "resolution_w", "w", "video_width")
_HEIGHT_KEYS = ("height", "resolution_h", "h", "video_height")
_RESOLUTION_KEYS = ("resolution", "res", "video_resolution")
_TYPE_KEYS = ("type", "camera_type", "cameraType", "kind", "category")
_RTSP_KEYS = ("rtsp_url", "rtsp", "rtspUrl", "rtsp_uri", "url_rtsp", "source")
_HLS_KEYS = ("hls_url", "hls", "hlsUrl", "m3u8", "url_hls", "playlist")
_WHEP_KEYS = ("whep_url", "whep", "whepUrl", "webrtc", "url_whep")
_ONVIF_KEYS = ("onvif_url", "onvif", "onvifUrl")

# Top-level wrappers seen in the wild for "here is a list of things".
_LIST_WRAPPER_KEYS = ("cameras", "streams", "data", "items", "results", "feeds")

# Union of every key spelling we know how to read. Used only to decide whether a
# dict-of-dicts is a camera collection or an envelope that happens to be shaped
# like one — see _entries().
_RECOGNISED_KEYS = frozenset(
    _ID_KEYS + _NAME_KEYS + _LAT_KEYS + _LON_KEYS + _CODEC_KEYS
    + _WIDTH_KEYS + _HEIGHT_KEYS + _RESOLUTION_KEYS + _TYPE_KEYS
    + _RTSP_KEYS + _HLS_KEYS + _WHEP_KEYS + _ONVIF_KEYS
)


def _first(entry: dict[str, Any], keys: tuple[str, ...]) -> tuple[str | None, Any]:
    """First present, non-empty value among `keys`. Returns (key_used, value).

    The key is returned as well as the value so the caller can record which
    spelling this sandbox actually uses and exclude it from ``raw``.
    """
    for key in keys:
        if key in entry:
            value = entry[key]
            if value is not None and value != "":
                return key, value
    return None, None


def _as_float(value: Any) -> float | None:
    """Coerce to float, or None. Never raises.

    Coordinates arrive as numbers, as strings, and occasionally as the string
    "null". A malformed coordinate must degrade to "no location" — which the sync
    layer will report as a rejected row — rather than abort the whole catalogue.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    # NaN and infinities pass float() and then poison PostGIS. Reject via the
    # only test that catches all three without importing math.
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _split_resolution(value: Any) -> tuple[int | None, int | None]:
    """Parse "1920x1080" / "1920*1080" / "1920 X 1080" into a pair.

    Vendors and demo grids express resolution as a single string about as often
    as they use two fields, and the separator is not agreed on.
    """
    if value is None:
        return None, None
    text = str(value).strip().lower().replace("*", "x").replace("×", "x")
    if "x" not in text:
        return None, None
    left, _, right = text.partition("x")
    return _as_int(left.strip()), _as_int(right.strip())


def _entries(payload: Any) -> list[dict[str, Any]]:
    """Find the list of camera records inside whatever we were handed.

    Accepts a bare list, or a dict with the list under any of
    ``_LIST_WRAPPER_KEYS``, or a dict that *is* one camera, or a dict keyed by
    camera id (MediaMTX's ``/v3/paths/list`` is dict-shaped in some versions).
    Anything else is a genuine protocol failure and says so.
    """
    if isinstance(payload, list):
        return [e for e in payload if isinstance(e, dict)]

    if isinstance(payload, dict):
        for key in _LIST_WRAPPER_KEYS:
            candidate = payload.get(key)
            if isinstance(candidate, list):
                return [e for e in candidate if isinstance(e, dict)]
            # Some APIs nest one level deeper: {"data": {"cameras": [...]}}
            if isinstance(candidate, dict):
                nested = _entries(candidate)
                if nested:
                    return nested
        # A dict of id -> record. Fold the key in as an id so it is not lost.
        # Guarded by _RECOGNISED_KEYS: without that check, an envelope like
        # {"data": {}} folds into a camera whose id is the literal string "data",
        # i.e. tolerance manufacturing a camera out of nothing. Requiring each
        # value to carry at least one field we know how to read is what
        # distinguishes a collection from a wrapper.
        values = [v for v in payload.values() if isinstance(v, dict)]
        if (
            values
            and len(values) == len(payload)
            and all(_RECOGNISED_KEYS & set(v) for v in values)
        ):
            folded = []
            for key, value in payload.items():
                merged = dict(value)
                merged.setdefault("id", key)
                folded.append(merged)
            return folded
        # A single camera object, not a collection.
        if any(k in payload for k in _ID_KEYS):
            return [payload]
        return []

    raise FederationError(
        f"catalogue is neither a list nor an object (got {type(payload).__name__}); "
        f"{CATALOGUE_PATH} is not the endpoint we think it is"
    )


def parse_catalogue(payload: Any) -> list[ForeignCamera]:
    """Normalise a ``/api/ingest`` response into ForeignCamera records.

    Pure: no I/O, no config, no clock. That is what makes the tolerance above
    testable against hand-written fixtures, which is the only way to test it at
    all until the live shape is known.

    Entries with no resolvable identifier are skipped rather than synthesised an
    id for — a camera whose id we invented cannot be re-matched on the next sync,
    so it would duplicate itself every run.
    """
    cameras: list[ForeignCamera] = []
    for entry in _entries(payload):
        camera = parse_entry(entry)
        if camera is not None:
            cameras.append(camera)
    return cameras


def parse_entry(entry: dict[str, Any]) -> ForeignCamera | None:
    """One catalogue record. ``None`` when it carries no usable identifier."""
    consumed: set[str] = set()

    def take(keys: tuple[str, ...]) -> Any:
        key, value = _first(entry, keys)
        if key is not None:
            consumed.add(key)
        return value

    external_id = take(_ID_KEYS)
    if external_id is None:
        return None
    external_id = str(external_id).strip()
    if not external_id:
        return None

    name = take(_NAME_KEYS)
    latitude = _as_float(take(_LAT_KEYS))
    longitude = _as_float(take(_LON_KEYS))
    codec = normalise_codec(take(_CODEC_KEYS))

    width = _as_int(take(_WIDTH_KEYS))
    height = _as_int(take(_HEIGHT_KEYS))
    if width is None or height is None:
        # Only consult the combined form when the split form was absent, so a
        # grid that provides both does not have the authoritative pair
        # overwritten by a stale summary string.
        combined_w, combined_h = _split_resolution(take(_RESOLUTION_KEYS))
        width = width if width is not None else combined_w
        height = height if height is not None else combined_h

    camera_type = normalise_camera_type(take(_TYPE_KEYS))

    rtsp_url = take(_RTSP_KEYS)
    hls_url = take(_HLS_KEYS)
    whep_url = take(_WHEP_KEYS)
    onvif_url = take(_ONVIF_KEYS)

    return ForeignCamera(
        external_id=external_id,
        name=str(name).strip() if name else external_id,
        latitude=latitude,
        longitude=longitude,
        codec=codec,
        resolution_w=width,
        resolution_h=height,
        camera_type=camera_type,
        rtsp_url=_clean_url(rtsp_url),
        hls_url=_clean_url(hls_url),
        whep_url=_clean_url(whep_url),
        onvif_url=_clean_url(onvif_url),
        # Everything we did not understand. This is the specification for the
        # stricter parser that should replace this one.
        raw={k: v for k, v in entry.items() if k not in consumed},
    )


def _clean_url(value: Any) -> str | None:
    """Keep a URL only if it looks like one.

    ``source`` in a MediaMTX-shaped payload is sometimes a real ``rtsp://`` URL
    and sometimes a descriptor like ``"publisher"`` or ``"rpiCamera"``. Storing
    the latter in ``camera.rtsp_url`` would give the viewer a string it will try
    to dial.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if "://" not in text:
        return None
    return text


class SentinelGridDriver(VmsDriver):
    """The hackathon sandbox camera grid.

    Config:

    ==================  =====================================================
    ``base_url``        required, e.g. ``http://10.20.30.40:8080``
    ``timeout``         seconds for the catalogue fetch (default 5)
    ``rtsp_pattern``    override for the RTSP fallback pattern
    ``whep_pattern``    override for the WHEP fallback pattern
    ``hls_pattern``     override for the HLS fallback pattern
    ``headers``         extra request headers, e.g. an API key
    ==================  =====================================================
    """

    platform: ClassVar[str] = "sentinel_grid"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        raw_base = str(self._require("base_url")).rstrip("/")
        if "://" not in raw_base:
            # Guessing the scheme would mean guessing whether credentials are
            # about to cross the network in the clear.
            raise FederationError(
                f"base_url must include a scheme, got {raw_base!r}"
            )
        self.base_url = raw_base
        parts = urllib.parse.urlsplit(raw_base)
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self.host = parts.hostname or ""
        if not self.host:
            raise FederationError(f"could not extract a host from base_url {raw_base!r}")

        self.rtsp_pattern = str(self.config.get("rtsp_pattern") or DEFAULT_RTSP_PATTERN)
        self.whep_pattern = str(self.config.get("whep_pattern") or DEFAULT_WHEP_PATTERN)
        self.hls_pattern = str(self.config.get("hls_pattern") or DEFAULT_HLS_PATTERN)

        self._client: Any | None = None
        # Catalogue URLs beat constructed ones, so the last fetch is remembered
        # and consulted by stream_url before it falls back to a pattern.
        self._cache: dict[str, ForeignCamera] = {}

    # -- transport ---------------------------------------------------------

    def _http(self) -> Any:
        """Lazily build the httpx client.

        Imported here rather than at module scope on purpose: it keeps
        ``parse_catalogue`` — the part with all the risk in it — importable and
        unit-testable on a machine with no third-party packages installed, which
        during a hackathon is most machines. The cost is one dict lookup per
        request.
        """
        if self._client is None:
            try:
                import httpx  # noqa: PLC0415 - see docstring
            except ImportError as exc:  # pragma: no cover - httpx is in requirements.txt
                # Translated rather than propagated: a bare ModuleNotFoundError
                # surfacing from inside a URL lookup tells the caller nothing
                # about which layer failed, and no caller catches ImportError.
                raise FederationError(
                    "httpx is required to reach the sandbox catalogue; install "
                    "services/registry/requirements.txt"
                ) from exc
            headers = {"Accept": "application/json"}
            extra = self.config.get("headers")
            if isinstance(extra, dict):
                headers.update({str(k): str(v) for k, v in extra.items()})
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                # Explicit and finite. httpx's default is 5s but relying on a
                # library default for a probe that runs 80,000 times is how a
                # version bump becomes an outage.
                timeout=self.timeout,
                headers=headers,
                follow_redirects=True,
            )
        return self._client

    async def _get_catalogue(self) -> Any:
        """GET the catalogue and return decoded JSON. The only network call here."""
        # _http() first: it is the one place that translates a missing httpx into
        # a FederationError, so by the time the import below runs it cannot fail.
        client = self._http()
        import httpx  # noqa: PLC0415 - see _http

        try:
            response = await client.get(CATALOGUE_PATH)
        except httpx.TimeoutException as exc:
            raise UpstreamUnavailable(
                f"sandbox catalogue timed out after {self.timeout}s: {self.base_url}"
            ) from exc
        except httpx.HTTPError as exc:
            # Covers connect errors, DNS, protocol errors, TLS. All transient by
            # assumption; the caller retries with backoff.
            raise UpstreamUnavailable(f"sandbox catalogue unreachable: {exc}") from exc

        if response.status_code in (401, 403):
            raise AuthenticationFailed(
                f"sandbox catalogue rejected our request ({response.status_code}); "
                "an API key may be required in config['headers']"
            )
        if response.status_code == 404:
            raise FederationError(
                f"{self.base_url}{CATALOGUE_PATH} returned 404 — wrong base_url, or "
                "the grid moved the catalogue"
            )
        if response.status_code >= 400:
            raise UpstreamUnavailable(
                f"sandbox catalogue returned HTTP {response.status_code}"
            )

        try:
            return response.json()
        except ValueError as exc:
            # An HTML error page from a reverse proxy is the usual cause. Include
            # a short prefix so the operator can see what answered.
            preview = response.text[:120].replace("\n", " ")
            raise FederationError(
                f"sandbox catalogue was not JSON: {preview!r}"
            ) from exc

    # -- contract ----------------------------------------------------------

    async def list_cameras(self) -> list[ForeignCamera]:
        cameras = parse_catalogue(await self._get_catalogue())
        self._cache = {c.external_id: c for c in cameras}
        return cameras

    async def stream_url(
        self, vms_camera_id: str, *, protocol: StreamProtocol
    ) -> StreamDescriptor:
        """Prefer the catalogue's URL; construct one only as a fallback.

        The cache is filled on first use if empty, so a caller that only wants
        one URL still gets the catalogue's answer rather than a guess. If the
        camera is absent from the catalogue we still construct a URL: the grid has
        been observed to serve paths it does not advertise, and refusing here
        would turn a working stream into a dead console tile.
        """
        if not self._cache:
            try:
                await self.list_cameras()
            except FederationError:
                # A guessed URL that might work beats no URL at all; the
                # consumer's own connection attempt is the real test.
                pass

        camera = self._cache.get(vms_camera_id)
        published = None
        if camera is not None:
            published = {
                "rtsp": camera.rtsp_url,
                "hls": camera.hls_url,
                "whep": camera.whep_url,
            }.get(protocol)

        if published:
            return StreamDescriptor(
                url=published,
                protocol=protocol,
                requires_credentials=False,
                detail="published by the sandbox catalogue",
            )

        url = self._construct_url(vms_camera_id, protocol)
        return StreamDescriptor(
            url=url,
            protocol=protocol,
            requires_credentials=False,
            detail=(
                "constructed from the documented fallback pattern; the catalogue "
                "did not publish this protocol for this camera"
            ),
        )

    def _construct_url(self, vms_camera_id: str, protocol: StreamProtocol) -> str:
        pattern = {
            "rtsp": self.rtsp_pattern,
            "hls": self.hls_pattern,
            "whep": self.whep_pattern,
        }.get(protocol)
        if pattern is None:
            raise NotSupported(f"unknown protocol {protocol!r}")
        # Path-quote the id: a stream name with a space or a slash in it would
        # otherwise produce a URL that means something different.
        quoted = urllib.parse.quote(str(vms_camera_id), safe="")
        return pattern.format(host=self.host, origin=self.origin, id=quoted)

    async def recording_url(
        self, vms_camera_id: str, start: datetime, end: datetime
    ) -> StreamDescriptor | None:
        """The sandbox grid is live-only.

        ``NotSupported`` rather than ``None``: ``None`` would mean "there is an
        archive and it holds nothing for that window", which would be a lie the
        console would render as a legitimately empty timeline. The grid publishes
        no VOD or playback endpoint at all.
        """
        raise NotSupported(
            "the sandbox camera grid exposes no recording or playback endpoint; "
            "archive retrieval is a real-VMS capability (see milestone.py, genetec.py)"
        )

    async def health(self, vms_camera_id: str) -> DriverHealth:
        """Catalogue-level liveness: is this id still being published?

        This deliberately does *not* touch the stream. It answers "does the grid
        still list this camera", which is the strongest claim obtainable without
        opening a media session. Per-stream liveness — is there an RTSP server
        answering on 8554 for this path — is ``onvif_rtsp``'s job, and the two are
        recorded as different ``probe`` kinds so the distinction survives into
        ``app.camera_health_check``.
        """
        started = time.monotonic()
        try:
            payload = await self._get_catalogue()
        except UpstreamUnavailable as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            code = ERR_TIMEOUT if "timed out" in str(exc) else ERR_UPSTREAM
            return DriverHealth(
                is_live=False, probe=PROBE_HTTP, latency_ms=elapsed,
                error_code=code, detail=str(exc)[:500],
            )
        except AuthenticationFailed as exc:
            return DriverHealth(
                is_live=False, probe=PROBE_HTTP,
                latency_ms=int((time.monotonic() - started) * 1000),
                error_code="auth", detail=str(exc)[:500],
            )
        except FederationError as exc:
            return DriverHealth(
                is_live=False, probe=PROBE_HTTP,
                latency_ms=int((time.monotonic() - started) * 1000),
                error_code=ERR_PROTOCOL, detail=str(exc)[:500],
            )

        elapsed_ms = int((time.monotonic() - started) * 1000)
        cameras = parse_catalogue(payload)
        self._cache = {c.external_id: c for c in cameras}
        if vms_camera_id in self._cache:
            return DriverHealth(
                is_live=True, probe=PROBE_HTTP, latency_ms=elapsed_ms,
                detail="listed in the sandbox catalogue",
            )
        return DriverHealth(
            is_live=False, probe=PROBE_HTTP, latency_ms=elapsed_ms,
            error_code=ERR_NOT_FOUND,
            detail=(
                f"{vms_camera_id!r} is not in the catalogue "
                f"({len(self._cache)} cameras listed)"
            ),
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
