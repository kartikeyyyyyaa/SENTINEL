"""Direct-connect adapter: one camera, or one departmental VMS, over ONVIF + RTSP.

This is the Model 2 ingestion path expressed as a Model 3 driver, and it is the
one that will actually be used for most of Gujarat's fleet. The great majority of
deployed cameras are not behind a branded VMS at all; they are an IP camera and an
NVR on a departmental LAN, speaking RTSP and — if you are lucky — ONVIF Profile S.

**There is no ONVIF SOAP stack here, on purpose.** A conformant ONVIF client needs
WS-Discovery, WS-Security UsernameToken digests with clock-skew correction, media
profile enumeration and a WSDL-shaped request for each operation. That is weeks of
work whose only output, for our purposes, would be a stream URI we already have in
``app.camera.rtsp_url``. What this module implements instead is the small part that
earns its keep: a liveness probe that is cheap enough to run across 80,000 cameras
and that **never establishes a media session**.

**The probe stops before ``SETUP``.** The RTSP handshake is
``OPTIONS`` → ``DESCRIBE`` → ``SETUP`` → ``PLAY``. ``SETUP`` is the point at which
the server allocates a session and begins reserving transport for media; ``PLAY``
starts the bytes flowing. We send ``OPTIONS``, read the status line, and hang up.
Cost per probe: one TCP connection and about 80 bytes. Cost of the same sweep
using ``DESCRIBE`` + ``SETUP``: 80,000 server-side sessions, many of which will be
on hardware that tops out at four concurrent clients — the health check would take
the fleet down. This is also why ``camera_health_check.probe`` distinguishes
``rtsp_options`` from ``rtsp_describe``: the two make different claims and cost
different amounts.
"""
from __future__ import annotations

import asyncio
import socket
import time
import urllib.parse
from datetime import datetime
from typing import Any, ClassVar

from .base import (
    ERR_AUTH,
    ERR_DNS,
    ERR_PROTOCOL,
    ERR_REFUSED,
    ERR_TIMEOUT,
    ERR_UNREACHABLE,
    ERR_UPSTREAM,
    PROBE_ONVIF,
    PROBE_RTSP_OPTIONS,
    DriverHealth,
    FederationError,
    ForeignCamera,
    NotSupported,
    StreamDescriptor,
    StreamProtocol,
    VmsDriver,
    normalise_camera_type,
    normalise_codec,
)

DEFAULT_RTSP_PORT = 554

# The ONVIF Device Management call every conformant device must answer *without*
# authentication. That requirement is not a courtesy: WS-Security UsernameToken
# digests are computed over the device's own clock, so a client has to be able to
# read that clock before it can authenticate anything. Which makes this the one
# ONVIF operation usable as an unauthenticated liveness probe — a device that
# answers this is alive and is an ONVIF device, and we learned it without holding
# a credential.
ONVIF_GET_SYSTEM_DATE_AND_TIME = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
    "<s:Body>"
    '<GetSystemDateAndTime xmlns="http://www.onvif.org/ver10/device/wsdl"/>'
    "</s:Body>"
    "</s:Envelope>"
)

ONVIF_SOAP_HEADERS = {
    "Content-Type": "application/soap+xml; charset=utf-8",
    # ONVIF is SOAP 1.2 over HTTP; the action goes in the Content-Type in 1.2,
    # but devices in the field are lenient and many still read SOAPAction.
    "SOAPAction": "http://www.onvif.org/ver10/device/wsdl/GetSystemDateAndTime",
}

_USER_AGENT = "Sentinel-HealthProbe/1.0"


def parse_rtsp_status_line(line: str | bytes) -> tuple[int, str]:
    """Parse ``RTSP/1.0 200 OK`` into ``(200, "OK")``.

    Raises ``FederationError`` on anything that is not an RTSP status line. The
    strictness is the point: the most informative failure this probe can report is
    "something is listening on 554 but it is not an RTSP server". A tolerant
    parser that shrugged at ``HTTP/1.1 200 OK`` would record that camera as
    healthy when what is really there is a web interface, a port-forward pointing
    at the wrong host, or a captive portal.

    A missing reason phrase is accepted — it is optional in RFC 2326 — and comes
    back as an empty string.
    """
    if isinstance(line, bytes):
        # Status lines are ASCII by specification. A device emitting something
        # else has already told us it is not speaking RTSP correctly, so decode
        # strictly and let the failure surface here.
        try:
            line = line.decode("ascii")
        except UnicodeDecodeError as exc:
            raise FederationError("RTSP status line was not ASCII") from exc

    text = line.strip()
    if not text:
        raise FederationError("empty RTSP status line (connection closed silently)")

    parts = text.split(None, 2)
    version = parts[0]
    if not version.upper().startswith("RTSP/"):
        raise FederationError(
            f"not an RTSP status line: {text[:60]!r} — something else is "
            "listening on this port"
        )
    if len(parts) < 2:
        raise FederationError(f"RTSP status line has no status code: {text[:60]!r}")

    try:
        code = int(parts[1])
    except ValueError as exc:
        raise FederationError(
            f"RTSP status code is not a number: {parts[1]!r}"
        ) from exc
    if not 100 <= code <= 599:
        raise FederationError(f"RTSP status code out of range: {code}")

    reason = parts[2].strip() if len(parts) > 2 else ""
    return code, reason


def _split_rtsp_target(url: str) -> tuple[str, int, str]:
    """(host, port, request_uri) for an RTSP URL.

    The request URI is rebuilt **without userinfo**. Two reasons: RFC 2326 does
    not provide for credentials in a Request-URI, and a camera's own access log is
    not somewhere we want the state's RTSP passwords to end up.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in ("rtsp", "rtsps"):
        raise FederationError(f"not an RTSP URL: {url!r}")
    host = parts.hostname
    if not host:
        raise FederationError(f"RTSP URL has no host: {url!r}")
    port = parts.port or DEFAULT_RTSP_PORT
    netloc = f"{host}:{port}"
    request_uri = urllib.parse.urlunsplit(
        (parts.scheme, netloc, parts.path or "/", parts.query, "")
    )
    return host, port, request_uri


class OnvifRtspDriver(VmsDriver):
    """A camera or NVR reached directly, with no vendor middleware in between.

    Config:

    ========================  ===============================================
    ``rtsp_url``              the live stream URL. Required for RTSP probing
                              and for ``stream_url``.
    ``onvif_url``             device service endpoint, e.g.
                              ``http://10.1.2.3/onvif/device_service``. Used
                              for the fallback probe.
    ``hls_url`` ``whep_url``  set only when a gateway republishes this camera.
    ``timeout``               seconds per probe (default 5).
    ``requires_credentials``  default True; see ``stream_url``.
    ``camera``                dict of inventory metadata for ``list_cameras``.
    ========================  ===============================================

    PTZ is not implemented and inherits the base class's refusal. Continuous move
    needs the WS-Security digest flow and a media profile token, i.e. the SOAP
    stack this module exists to avoid. When PTZ becomes a requirement the honest
    options are a real ONVIF client library or a per-vendor HTTP CGI driver — not
    a half-built SOAP layer in here.
    """

    platform: ClassVar[str] = "onvif_rtsp"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self.rtsp_url: str | None = self.config.get("rtsp_url") or None
        self.onvif_url: str | None = self.config.get("onvif_url") or None
        self.hls_url: str | None = self.config.get("hls_url") or None
        self.whep_url: str | None = self.config.get("whep_url") or None
        if not self.rtsp_url and not self.onvif_url:
            raise FederationError(
                "onvif_rtsp requires at least one of 'rtsp_url' or 'onvif_url'"
            )
        # Defaults to True because the conservative direction is to make the
        # consumer go through the audited credential path. A false negative here
        # means an unauthenticated stream is fetched with an unnecessary
        # credential lookup, which is merely wasteful; a false positive means the
        # console tries to play a stream with no credential and shows the
        # operator a black tile with no explanation.
        self._requires_credentials = bool(self.config.get("requires_credentials", True))
        self._client: Any | None = None

    # -- probes ------------------------------------------------------------

    async def _probe_rtsp_options(self, url: str) -> DriverHealth:
        """TCP connect, one ``OPTIONS``, read the status line, disconnect.

        Latency is measured across connect *and* the request/response round trip,
        because that combined figure is what the operator experiences as "how slow
        is this camera" — a camera on a congested 4G backhaul can connect quickly
        and then take two seconds to answer.
        """
        try:
            host, port, request_uri = _split_rtsp_target(url)
        except FederationError as exc:
            # Misconfiguration, not a camera fault. Still returned as a health
            # row rather than raised: the sweep must not stop because one row in
            # the inventory has a typo in it.
            return DriverHealth(
                is_live=False, probe=PROBE_RTSP_OPTIONS,
                error_code=ERR_PROTOCOL, detail=str(exc),
            )

        request = (
            f"OPTIONS {request_uri} RTSP/1.0\r\n"
            "CSeq: 1\r\n"
            f"User-Agent: {_USER_AGENT}\r\n"
            "\r\n"
        ).encode("ascii")

        started = time.monotonic()
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=self.timeout
            )
            writer.write(request)
            await asyncio.wait_for(writer.drain(), timeout=self.timeout)
            # readline, not read: we want exactly the status line and then out.
            # Reading to EOF would wait for a server that keeps the connection
            # open for a session that we are never going to start.
            raw_line = await asyncio.wait_for(reader.readline(), timeout=self.timeout)
            elapsed_ms = int((time.monotonic() - started) * 1000)

        # Ordering and the tuple below are both load-bearing.
        #
        # On Python 3.11+ asyncio.TimeoutError IS the builtin TimeoutError, which
        # is a subclass of OSError — so an `except OSError` placed first would
        # swallow timeouts and misreport them as unreachable. On 3.10 the two are
        # *different* classes and the builtin one is what the socket layer raises
        # for ETIMEDOUT, so catching only asyncio's would let a genuine connect
        # timeout fall through to the OSError branch. Catching both, first, is the
        # only form that behaves identically on either interpreter.
        #
        # It matters because timeout and unreachable are different faults with
        # different fixes: congestion or a silently dropping firewall, versus a
        # wrong address or a missing route.
        except (asyncio.TimeoutError, TimeoutError):
            return DriverHealth(
                is_live=False, probe=PROBE_RTSP_OPTIONS,
                latency_ms=int((time.monotonic() - started) * 1000),
                error_code=ERR_TIMEOUT,
                detail=f"no RTSP response from {host}:{port} within {self.timeout}s",
            )
        except ConnectionRefusedError:
            # Refused is the *useful* failure: something answered, and it said no.
            # The host is up and routable; the RTSP service is not running.
            return DriverHealth(
                is_live=False, probe=PROBE_RTSP_OPTIONS,
                latency_ms=int((time.monotonic() - started) * 1000),
                error_code=ERR_REFUSED,
                detail=f"{host}:{port} refused the connection (host up, RTSP down)",
            )
        except socket.gaierror as exc:
            return DriverHealth(
                is_live=False, probe=PROBE_RTSP_OPTIONS,
                error_code=ERR_DNS, detail=f"cannot resolve {host!r}: {exc}",
            )
        except OSError as exc:
            # Network unreachable, no route to host, connection reset, TLS
            # failures. Genuinely "we could not get there".
            return DriverHealth(
                is_live=False, probe=PROBE_RTSP_OPTIONS,
                latency_ms=int((time.monotonic() - started) * 1000),
                error_code=ERR_UNREACHABLE, detail=f"{host}:{port}: {exc}",
            )
        finally:
            if writer is not None:
                writer.close()
                # Deliberately not awaiting wait_closed(): the peer may hold the
                # socket open, and a probe must not block on the far end's
                # politeness. The transport is dropped either way.

        try:
            code, reason = parse_rtsp_status_line(raw_line)
        except FederationError as exc:
            return DriverHealth(
                is_live=False, probe=PROBE_RTSP_OPTIONS, latency_ms=elapsed_ms,
                error_code=ERR_PROTOCOL, detail=str(exc),
            )

        # A well-formed RTSP status line is itself proof of life, whatever the
        # code says. 401 in particular means the server is running and demanding
        # Digest auth — the camera is emphatically alive, and recording it as down
        # would send an engineer to a working site. The credential problem is
        # reported through error_code instead, which is exactly the signal the
        # "cameras still on vendor default passwords" report needs.
        if code == 401:
            return DriverHealth(
                is_live=True, probe=PROBE_RTSP_OPTIONS, latency_ms=elapsed_ms,
                error_code=ERR_AUTH,
                detail="RTSP 401: server alive, credentials required or wrong",
            )
        if code >= 500:
            return DriverHealth(
                is_live=False, probe=PROBE_RTSP_OPTIONS, latency_ms=elapsed_ms,
                error_code=ERR_UPSTREAM, detail=f"RTSP {code} {reason}".strip(),
            )
        return DriverHealth(
            is_live=True, probe=PROBE_RTSP_OPTIONS, latency_ms=elapsed_ms,
            detail=f"RTSP {code} {reason}".strip(),
        )

    async def _probe_onvif(self, url: str) -> DriverHealth:
        """POST ``GetSystemDateAndTime`` over HTTP and see if a device answers.

        Recorded as probe kind ``onvif`` rather than ``http``: the request carries
        a Device Management operation, so a response proves more than "a socket
        accepted bytes" — it proves an ONVIF device is there. The plain ``http``
        probe kind is reserved for probes that only establish that a web server
        exists.

        A SOAP Fault counts as alive. A device replying "I do not like your
        request" has, in replying, answered the only question being asked.
        """
        try:
            import httpx  # noqa: PLC0415 - keeps this module importable without httpx
        except ImportError as exc:  # pragma: no cover - httpx is in requirements.txt
            # Translated, not propagated: nothing upstream catches ImportError,
            # and the RTSP probe above needs no third-party package at all, so a
            # missing httpx must degrade only the ONVIF fallback.
            raise FederationError(
                "httpx is required for the ONVIF fallback probe; install "
                "services/registry/requirements.txt"
            ) from exc

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)

        started = time.monotonic()
        try:
            response = await self._client.post(
                url, content=ONVIF_GET_SYSTEM_DATE_AND_TIME, headers=ONVIF_SOAP_HEADERS
            )
        except httpx.TimeoutException:
            return DriverHealth(
                is_live=False, probe=PROBE_ONVIF,
                latency_ms=int((time.monotonic() - started) * 1000),
                error_code=ERR_TIMEOUT,
                detail=f"no ONVIF response from {url} within {self.timeout}s",
            )
        except httpx.HTTPError as exc:
            return DriverHealth(
                is_live=False, probe=PROBE_ONVIF,
                latency_ms=int((time.monotonic() - started) * 1000),
                error_code=ERR_UNREACHABLE, detail=f"{url}: {exc}",
            )

        elapsed_ms = int((time.monotonic() - started) * 1000)
        if response.status_code == 401:
            # Unexpected — this call is meant to be unauthenticated — but still
            # conclusive about liveness, and worth flagging because it means the
            # device is non-conformant and every other ONVIF call will need auth.
            return DriverHealth(
                is_live=True, probe=PROBE_ONVIF, latency_ms=elapsed_ms,
                error_code=ERR_AUTH,
                detail="device demands auth for GetSystemDateAndTime (non-conformant)",
            )
        if response.status_code >= 500 and "Envelope" not in response.text[:400]:
            # 500 with a SOAP Envelope is a Fault, i.e. a real ONVIF device. 500
            # without one is a broken web server.
            return DriverHealth(
                is_live=False, probe=PROBE_ONVIF, latency_ms=elapsed_ms,
                error_code=ERR_UPSTREAM, detail=f"HTTP {response.status_code}, no SOAP body",
            )
        if "Envelope" not in response.text[:400]:
            return DriverHealth(
                is_live=False, probe=PROBE_ONVIF, latency_ms=elapsed_ms,
                error_code=ERR_PROTOCOL,
                detail=f"HTTP {response.status_code} but no SOAP envelope in the reply",
            )
        return DriverHealth(
            is_live=True, probe=PROBE_ONVIF, latency_ms=elapsed_ms,
            detail=f"ONVIF GetSystemDateAndTime answered (HTTP {response.status_code})",
        )

    # -- contract ----------------------------------------------------------

    async def health(self, vms_camera_id: str) -> DriverHealth:
        """RTSP ``OPTIONS`` first; ONVIF over HTTP as a fallback.

        RTSP goes first because it probes the thing we actually care about — the
        media service on the port the viewer will dial. ONVIF is the fallback
        because a camera can have a perfectly healthy management interface while
        its RTSP server is wedged, and reporting the former as the latter is the
        failure mode that makes a status map lie.

        ``vms_camera_id`` is accepted for signature compatibility and ignored: a
        direct connection is to one camera, and the URL in config identifies it.
        """
        if self.rtsp_url:
            result = await self._probe_rtsp_options(self.rtsp_url)
            if result.is_live or not self.onvif_url:
                return result
            # RTSP failed and we have a management endpoint: find out whether the
            # whole device is down or only its media service. The distinction
            # decides whether this is a site visit or a remote restart.
            fallback = await self._probe_onvif(self.onvif_url)
            if fallback.is_live:
                return DriverHealth(
                    is_live=False,
                    probe=PROBE_ONVIF,
                    latency_ms=fallback.latency_ms,
                    error_code=ERR_UPSTREAM,
                    detail=(
                        "device reachable over ONVIF but its RTSP service is not "
                        f"answering ({result.error_code}: {result.detail})"
                    ),
                )
            return fallback

        if self.onvif_url:
            return await self._probe_onvif(self.onvif_url)
        raise FederationError("no rtsp_url or onvif_url configured")  # pragma: no cover

    async def stream_url(
        self, vms_camera_id: str, *, protocol: StreamProtocol
    ) -> StreamDescriptor:
        """Return the configured URL, appending nothing.

        Nothing is appended and nothing is rewritten. Transport and decode options
        belong to the consumer, and encoding them into the URL here would bake one
        client's needs into a value that three different clients read.

        **Consumers must force ``rtsp_transport=tcp``.** This is not a
        preference. Over the state WAN, RTP over UDP reorders and silently drops
        packets, which in a mixed H.264/H.265 grid shows up as a decoder that
        stalls on a missing reference frame and never recovers — a frozen tile
        that still looks like a live tile. TCP costs latency and gains a stream
        that either works or visibly fails. In ffmpeg/OpenCV terms:
        ``OPENCV_FFMPEG_CAPTURE_OPTIONS=rtsp_transport;tcp``, or
        ``-rtsp_transport tcp`` on the command line.
        """
        url = {
            "rtsp": self.rtsp_url,
            "hls": self.hls_url,
            "whep": self.whep_url,
        }.get(protocol)
        if not url:
            raise NotSupported(
                f"no {protocol} URL configured for this direct-connect camera; "
                "a bare IP camera speaks RTSP only unless a gateway republishes it"
            )
        return StreamDescriptor(
            url=url,
            protocol=protocol,
            # No expiry: a camera's RTSP URL is static. Nothing here mints a
            # token, so there is nothing to expire.
            expires_at=None,
            requires_credentials=self._requires_credentials if protocol == "rtsp" else False,
            detail=(
                "force rtsp_transport=tcp" if protocol == "rtsp" else ""
            ),
        )

    async def list_cameras(self) -> list[ForeignCamera]:
        """A direct camera is its own inventory: one record, or nothing.

        Raises ``NotSupported`` when configured without a ``camera`` block,
        because returning an empty list would read to the sync layer as "this
        platform has no cameras" and could retire rows that are perfectly fine.
        Discovering more than one camera would require WS-Discovery, which is
        multicast and therefore useless across the routed segments this fleet
        actually lives on.
        """
        meta = self.config.get("camera")
        if not isinstance(meta, dict) or not meta:
            raise NotSupported(
                "onvif_rtsp cannot enumerate: a direct connection has no camera "
                "catalogue. Provide config['camera'] with the inventory metadata, "
                "or onboard this camera through the bulk-import path instead."
            )
        external_id = str(
            meta.get("external_id") or meta.get("id") or self.config.get("vms_camera_id") or ""
        ).strip()
        if not external_id:
            raise FederationError("config['camera'] needs an 'external_id'")

        consumed = {
            "external_id", "id", "name", "latitude", "longitude", "codec",
            "resolution_w", "resolution_h", "camera_type",
        }
        return [
            ForeignCamera(
                external_id=external_id,
                name=str(meta.get("name") or external_id),
                latitude=_maybe_float(meta.get("latitude")),
                longitude=_maybe_float(meta.get("longitude")),
                codec=normalise_codec(meta.get("codec")),
                resolution_w=_maybe_int(meta.get("resolution_w")),
                resolution_h=_maybe_int(meta.get("resolution_h")),
                camera_type=normalise_camera_type(meta.get("camera_type")),
                rtsp_url=self.rtsp_url,
                hls_url=self.hls_url,
                whep_url=self.whep_url,
                onvif_url=self.onvif_url,
                raw={k: v for k, v in meta.items() if k not in consumed},
            )
        ]

    async def recording_url(
        self, vms_camera_id: str, start: datetime, end: datetime
    ) -> StreamDescriptor | None:
        """Not implemented, and not fakeable.

        ONVIF Profile G defines replay: ``GetRecordings`` and ``GetReplayUri`` on
        the Recording and Replay services, then an RTSP session with a
        ``Range: clock=...`` header. All of it is authenticated SOAP, i.e. the
        stack this module exists to avoid, and Profile G support on the low-cost
        cameras that dominate this fleet is rare enough that building it
        speculatively would be building for a minority. When archive retrieval is
        needed, it comes from the NVR's own API through its own driver.
        """
        raise NotSupported(
            "onvif_rtsp does not implement ONVIF Profile G replay; archive "
            "retrieval requires an NVR- or VMS-specific driver"
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _maybe_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _maybe_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
