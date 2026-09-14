"""The VMS driver contract.

**Model 3 normalises metadata only. Video never flows through this layer.**

A driver answers questions — what cameras exist, where they are, what URL plays
this one, is it alive — and returns URLs and descriptors. The operator's browser
or an edge analytics worker then connects to those URLs *directly*. No frame of
video is ever proxied, transcoded or buffered by this service.

That is not a simplification; it is the load-bearing design decision. Gujarat's
target fleet is on the order of 80,000 cameras. At a conservative 2 Mbps per
stream, relaying all of them through a central tier would require roughly
**160 Gbps sustained** ingress *and* the same again egress, and 30-day retention
of that ingress is about **52 PB**. Both numbers are an order of magnitude
outside what a hackathon-to-production path can honestly promise, and neither
buys anything: the video is already sitting on departmental NVRs, reachable over
the state network. So this layer moves the *addresses* of video, which are
kilobytes, and leaves the video where it is.

Two consequences worth stating explicitly, because they shape every driver:

* **Health checks must not open a media session.** Liveness is established with a
  TCP connect, an RTSP ``OPTIONS`` exchange that stops before ``SETUP``, or an
  ONVIF ``GetSystemDateAndTime`` call. A probe that pulls a keyframe to "really"
  check the camera is a probe that, times 80,000, is a DDoS against your own
  network.
* **Drivers are configuration, not code.** Every driver is constructed from a
  plain dict, so onboarding a new department's VMS is a row of config plus, at
  most, one new module — never a change to the registry, the API or the console.

The vocabularies here (``probe``, ``codec``, ``camera_type``) are deliberately the
same closed sets as the ``CHECK`` constraints in ``db/migrations/002_camera.sql``.
A driver that invents a value fails *here*, at the adapter boundary with the
vendor's name attached, rather than as an opaque constraint violation halfway
through a 4,000-row sync.
"""
from __future__ import annotations

import abc
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar, Literal

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FederationError(Exception):
    """Base class for every fault this layer raises.

    Callers that only want "the federation layer failed" catch this. Nothing in
    this package raises a bare ``Exception``, and no driver is permitted to let a
    vendor SDK's own exception type escape — that would leak the vendor's error
    taxonomy into the API layer and make error handling unwritable.
    """


class NotSupported(FederationError):
    """This platform cannot do this, and no retry will change that.

    Distinct from ``UpstreamUnavailable`` because the correct response differs: a
    404/501 to the client, not a retry with backoff. Genuinely common — most
    fixed cameras have no PTZ and most sandbox streams have no archive.
    """


class UpstreamUnavailable(FederationError):
    """The VMS or camera did not answer, or answered unintelligibly.

    Transient by assumption. The caller may retry with backoff; it must not
    interpret this as "the camera does not exist".
    """


class AuthenticationFailed(FederationError):
    """Credentials were rejected, or are required and absent.

    Never carries the credential itself, or any part of it, for the same reason
    ``core.crypto.CryptoError`` does not: exception text ends up in logs.
    """


# ---------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------

StreamProtocol = Literal["rtsp", "hls", "whep"]

#: Probe kinds, matching ``app.camera_health_check.probe``'s CHECK constraint.
PROBE_TCP = "tcp"
PROBE_RTSP_OPTIONS = "rtsp_options"
PROBE_RTSP_DESCRIBE = "rtsp_describe"
PROBE_ONVIF = "onvif"
PROBE_HTTP = "http"
PROBES = frozenset(
    {PROBE_TCP, PROBE_RTSP_OPTIONS, PROBE_RTSP_DESCRIBE, PROBE_ONVIF, PROBE_HTTP}
)

# Error codes are a closed vocabulary so the map's status layer can colour by
# cause and the ageing-infrastructure report can count causes. Free-text detail
# carries the specifics; this field carries the class of failure.
ERR_TIMEOUT = "timeout"
ERR_REFUSED = "refused"
ERR_DNS = "dns"
ERR_UNREACHABLE = "unreachable"
ERR_PROTOCOL = "protocol"
ERR_AUTH = "auth"
ERR_NOT_FOUND = "not_found"
ERR_UPSTREAM = "upstream"

#: ``app.camera.codec``'s CHECK constraint, plus the spellings vendors actually
#: emit. Values are what goes in the column.
_CODEC_ALIASES = {
    "h264": "h264",
    "h.264": "h264",
    "avc": "h264",
    "avc1": "h264",
    "mpeg4-avc": "h264",
    "h265": "h265",
    "h.265": "h265",
    "hevc": "h265",
    "hvc1": "h265",
    "mjpeg": "mjpeg",
    "m-jpeg": "mjpeg",
    "motion jpeg": "mjpeg",
    "jpeg": "mjpeg",
    "av1": "av1",
    "mpeg4": "mpeg4",
    "mpeg-4": "mpeg4",
    "mp4v": "mpeg4",
}

#: ``app.camera.camera_type``'s CHECK constraint.
CAMERA_TYPES = frozenset(
    {
        "fixed",
        "ptz",
        "dome",
        "bullet",
        "anpr",
        "thermal",
        "panoramic",
        "body_worn",
        "mobile",
    }
)

_CAMERA_TYPE_ALIASES = {
    "static": "fixed",
    "box": "fixed",
    "speed dome": "ptz",
    "pan-tilt-zoom": "ptz",
    "ptz dome": "ptz",
    "lpr": "anpr",
    "npr": "anpr",
    "anpr camera": "anpr",
    "number plate": "anpr",
    "ir": "thermal",
    "fisheye": "panoramic",
    "360": "panoramic",
    "multi-sensor": "panoramic",
    "bodycam": "body_worn",
    "body-worn": "body_worn",
    "bwc": "body_worn",
    "vehicle": "mobile",
    "dashcam": "mobile",
}


def normalise_codec(value: Any) -> str | None:
    """Map a vendor's codec string onto the column's vocabulary, or ``None``.

    ``None`` rather than a guess: ``codec`` is nullable, and the migration
    already warns that declared stream characteristics are unreliable inventory
    metadata. An unknown codec recorded as NULL is honest; recorded as 'h264'
    it is a fabrication that some later heuristic will trust.
    """
    if value is None:
        return None
    key = str(value).strip().lower()
    if not key:
        return None
    return _CODEC_ALIASES.get(key)


def normalise_camera_type(value: Any, default: str = "fixed") -> str:
    """Map a vendor's type string onto the column's vocabulary.

    Unlike codec this has a default, because ``camera_type`` is ``NOT NULL``.
    The default is ``fixed`` and that direction is chosen deliberately: 'fixed'
    is the *least* capable type, so an unknown camera is never advertised to the
    console as PTZ-capable or ANPR-capable. Under-claiming a capability produces
    a greyed-out button; over-claiming produces an operator whose control input
    silently does nothing during an incident.
    """
    if value is not None:
        key = str(value).strip().lower().replace("_", " ")
        if key.replace(" ", "_") in CAMERA_TYPES:
            return key.replace(" ", "_")
        if key in _CAMERA_TYPE_ALIASES:
            return _CAMERA_TYPE_ALIASES[key]
    return default


# ---------------------------------------------------------------------------
# Descriptors
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ForeignCamera:
    """One camera as a foreign VMS describes it, normalised onto our columns.

    Frozen because a driver's output is a snapshot of what upstream said at one
    instant. If the sync layer wants to amend a field it produces a new record,
    so what the vendor claimed stays distinguishable from what we decided.

    ``external_id`` is the driver-scoped identifier and lands in both
    ``camera.vms_camera_id`` and ``camera.external_id``; ``(vms_platform,
    vms_camera_id)`` is the join key back to this driver. ``raw`` keeps every
    field we did not recognise, so a vendor extension nobody anticipated is
    preserved rather than silently dropped, and so the parser can be tightened
    later against real observed payloads.
    """

    external_id: str
    name: str
    latitude: float | None
    longitude: float | None
    codec: str | None = None
    resolution_w: int | None = None
    resolution_h: int | None = None
    camera_type: str = "fixed"

    # Access paths, mapping 1:1 onto app.camera's URL columns. All optional: a
    # VMS that only re-streams on demand has none of them until asked.
    rtsp_url: str | None = None
    hls_url: str | None = None
    whep_url: str | None = None
    onvif_url: str | None = None

    # Free-form upstream extras. Also the audit trail for parser tolerance.
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_location(self) -> bool:
        """``app.camera.location`` is ``NOT NULL``; this is the gate for insert.

        Cameras without coordinates are still returned by drivers — an
        unlocatable camera is still an asset the state paid for, and dropping it
        in the parser would make it invisible to the very reconciliation report
        that should flag it.
        """
        return self.latitude is not None and self.longitude is not None


@dataclass(frozen=True, slots=True)
class StreamDescriptor:
    """Where to fetch media from, and what the consumer must know to do it.

    This is the entire payload of Model 3's hot path: a URL and the handful of
    facts a client needs to use it correctly. Note what is *absent* — no socket,
    no session handle, no byte stream. The consumer connects; we do not.
    """

    url: str
    protocol: StreamProtocol

    #: When this URL stops working, if upstream said. Consumers must re-request
    #: before this instant rather than discovering expiry as a stall mid-incident.
    #: ``None`` means "no stated expiry", not "never expires".
    expires_at: datetime | None = None

    #: True when the URL alone is insufficient and credentials must be attached
    #: (RTSP Digest, a bearer token, a signed cookie). The credential itself is
    #: never carried here: it comes from ``app.camera_credential`` via
    #: ``app.get_camera_credential``, which enforces the permission check and the
    #: audit row. A descriptor that embedded ``user:pass@`` in the URL would put
    #: camera passwords into browser history, proxy logs and Referer headers.
    requires_credentials: bool = False

    #: Operator- and developer-facing note, e.g. a transport requirement. Never
    #: parsed by anything.
    detail: str = ""

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at <= datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class DriverHealth:
    """The result of one liveness probe, shaped to be inserted as-is.

    Fields mirror ``app.camera_health_check`` exactly so the health worker is a
    straight INSERT with no translation step to get wrong.

    ``is_live`` answers "did something on the far end speak the protocol", which
    is a narrower claim than "the operator can watch this camera". Kept narrow on
    purpose: a probe that stops before ``SETUP`` cannot know whether media
    actually flows, and reporting more confidence than the probe earned is how a
    status map becomes untrustworthy.
    """

    is_live: bool
    probe: str
    latency_ms: int | None = None
    error_code: str | None = None
    detail: str = ""
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        # Fail at the adapter boundary, where the driver name is still in the
        # traceback, instead of as a CHECK violation during a bulk insert.
        if self.probe not in PROBES:
            raise FederationError(
                f"unknown probe {self.probe!r}; app.camera_health_check accepts "
                f"{sorted(PROBES)}"
            )
        if self.latency_ms is not None and self.latency_ms < 0:
            raise FederationError("latency_ms must be >= 0")


@dataclass(frozen=True, slots=True)
class PtzCommand:
    """A movement request in normalised units.

    Pan, tilt and zoom are fractions of the device's maximum velocity in
    ``[-1.0, 1.0]`` rather than degrees or vendor steps. Every vendor disagrees
    about units — ONVIF ContinuousMove takes a normalised velocity, Hikvision's
    ISAPI takes signed integer speeds, Genetec takes named speed levels — so the
    only unit that can survive the adapter boundary unambiguously is a fraction
    of full scale. Converting to the vendor's units is each driver's job.

    ``duration_ms`` exists because a continuous move with no stop is how a camera
    ends up parked at the sky: a driver that supports continuous motion must
    either honour the duration or issue its own stop.
    """

    action: Literal["pan_tilt", "zoom", "preset", "stop", "home"]
    pan: float = 0.0
    tilt: float = 0.0
    zoom: float = 0.0
    preset: str | None = None
    duration_ms: int | None = None

    def __post_init__(self) -> None:
        for name in ("pan", "tilt", "zoom"):
            value = getattr(self, name)
            if not -1.0 <= float(value) <= 1.0:
                raise FederationError(f"{name} must be within [-1.0, 1.0], got {value!r}")
        if self.action == "preset" and not self.preset:
            raise FederationError("action 'preset' requires a preset token")
        if self.duration_ms is not None and self.duration_ms <= 0:
            raise FederationError("duration_ms must be positive when given")


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


class VmsDriver(abc.ABC):
    """What every federated platform must be able to answer.

    Constructed from a config dict and nothing else. No driver reads settings,
    the database or the environment for itself: the caller resolves configuration
    (including decrypting credentials through ``core.crypto``) and hands over a
    plain mapping. That keeps drivers unit-testable without a database, and keeps
    the decision about *who is allowed to use these credentials* in one place
    rather than smeared across every adapter.

    Implementations must be safe to construct eagerly and cheaply — no I/O in
    ``__init__``. Connections are opened on first use and released by ``close()``.
    """

    #: The value stored in ``app.camera.vms_platform``. Also the registry key.
    platform: ClassVar[str] = ""

    #: Seconds. Overridable per driver via config; kept small because a stalled
    #: probe holding a worker slot is worse than a missing data point.
    default_timeout: ClassVar[float] = 5.0

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self.config: dict[str, Any] = dict(config or {})
        self.timeout = float(self.config.get("timeout", self.default_timeout))
        if self.timeout <= 0:
            raise FederationError("timeout must be positive")

    # -- configuration helpers ---------------------------------------------

    def _require(self, key: str) -> Any:
        """Fetch a mandatory config value or fail with a message naming both the
        key and the platform, because "KeyError: 'base_url'" in a log at 2 a.m.
        does not say which of eleven configured departments is misconfigured."""
        if key not in self.config or self.config[key] in (None, ""):
            raise FederationError(
                f"{type(self).__name__} ({self.platform}) requires config key {key!r}"
            )
        return self.config[key]

    # -- the contract ------------------------------------------------------

    @abc.abstractmethod
    async def list_cameras(self) -> list[ForeignCamera]:
        """Every camera this platform will admit to, normalised.

        Metadata only. Implementations must not open a media session to answer
        this, however tempting it is to read the real codec off the stream.
        """

    @abc.abstractmethod
    async def stream_url(
        self, vms_camera_id: str, *, protocol: StreamProtocol
    ) -> StreamDescriptor:
        """Where the *client* should connect for live media.

        Raises ``NotSupported`` if the platform cannot offer that protocol —
        which is the common case, not an error condition. Most departmental VMS
        deployments speak RTSP and nothing else; WHEP generally means someone has
        put MediaMTX or equivalent in front.
        """

    @abc.abstractmethod
    async def recording_url(
        self, vms_camera_id: str, start: datetime, end: datetime
    ) -> StreamDescriptor | None:
        """Where the client should connect for archived media over a window.

        Returns ``None`` when the platform is reachable and simply has no
        recording for that window — a legitimate answer, distinct from
        ``NotSupported`` (the platform has no archive API at all) and from
        ``UpstreamUnavailable`` (we could not ask).
        """

    @abc.abstractmethod
    async def health(self, vms_camera_id: str) -> DriverHealth:
        """Is this camera alive, established without consuming media.

        Returns rather than raises for the ordinary failures: "this camera is
        down" is a *result* that belongs in ``app.camera_health_check``, not an
        exception. Reserve exceptions for "we could not perform the check at
        all", e.g. the driver is misconfigured.
        """

    async def ptz(self, vms_camera_id: str, command: PtzCommand) -> None:
        """Move the camera. Default: refuse.

        Defaulting to refusal rather than to a silent no-op is the whole point.
        A no-op default means an operator drags a joystick during an incident,
        sees nothing move, and cannot tell whether the camera is stuck, the
        network is down, or the feature was never wired up.
        """
        raise NotSupported(f"{self.platform} driver does not implement PTZ control")

    @abc.abstractmethod
    async def close(self) -> None:
        """Release sockets and pooled connections. Must be idempotent."""

    # -- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> VmsDriver:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        # Config can hold credentials, so it is summarised by key, never dumped.
        return f"{type(self).__name__}(platform={self.platform!r}, config_keys={sorted(self.config)})"
