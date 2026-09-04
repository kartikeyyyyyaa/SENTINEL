"""Milestone XProtect — documented stub.

XProtect is the most likely branded VMS to be found behind a Gujarat departmental
deployment of any size, so it gets a named driver slot. What it does not get is a
fabricated implementation.

**Everything here raises ``NotSupported``.** No canned camera list, no plausible
looking RTSP URL, no synthetic health status. A stub that returns invented data is
worse than one that refuses, because the refusal is visible in one place at
integration time while the invention is invisible until an operator acts on it.
The mock-data version of this file would demo beautifully and then, on the first
real deployment, produce a console full of cameras that do not exist.

What this file is actually for: it proves the adapter framework is **open for
extension**. Adding a platform means one module implementing one seven-method
contract plus one line in ``registry.py`` — no change to the API layer, the
database, the console or any other driver. That property, not the number of
vendors currently supported, is the Model 3 deliverable. The stub is the evidence,
and it is honest evidence precisely because it does not pretend to work.

The real integration path
-------------------------

Two routes into XProtect, and the choice is not obvious:

1. **MIP SDK** — the full Milestone Integration Platform SDK. Complete access:
   configuration, live and playback via the Recording Server, events, bookmarks,
   PTZ. It is a **.NET** SDK, which for a Python service means either an
   out-of-process C# sidecar exposing a small HTTP surface, or hosting the CLR.
   Both are real work. Use this when the requirement includes playback with frame
   accuracy or configuration write-back.

2. **Mobile Server REST API + RTSP re-stream** — the interface the XProtect mobile
   and web clients use. Far cheaper to consume from Python, exposes camera lists,
   live and playback, and re-streams as RTSP or HLS through the Mobile Server so
   no Recording Server protocol implementation is needed. The trade-off is that
   the Mobile Server becomes a relay in the media path, which conflicts with this
   layer's whole premise — so if this route is taken, the Mobile Server must be
   deployed *at the department*, not centrally, or the 160 Gbps problem in
   ``base.py`` reappears wearing a Milestone badge.

There is also a third option worth pricing before either: XProtect ships an
**ONVIF Bridge** that republishes cameras as standard ONVIF/RTSP. If a department
enables it, ``onvif_rtsp.py`` already federates that site today, with no new code
and no SDK licence. For a pilot this is almost always the right first move.

Authentication
--------------

XProtect authentication is version-dependent and this matters more than the
transport choice:

* **Basic users** authenticate against the XProtect Identity Provider (OpenID
  Connect / OAuth 2.0), via a token endpoint under the IDP with a
  password grant. The returned bearer token is short-lived and must be refreshed;
  a driver that fetches one per request will be rate-limited.
* **Windows/AD users** authenticate with NTLM or Kerberos against the Management
  Server, which from Linux means either a delegated ticket or an AD service
  account — an infrastructure decision, not a code one.

Exact endpoint paths, grant parameters and client ids differ across XProtect
versions (2020 R1 introduced the IDP; earlier versions use a WCF login service).
They are deliberately not written down here as if they were known: they must be
confirmed against the specific deployment's version, and a hardcoded guess in this
file would be indistinguishable from a fact.

Which means the blocker is not code
------------------------------------

The blocker is **a live XProtect instance plus a service account with
``read`` on the cameras of interest**. Given those, this driver is on the order of
a day's work against the Mobile Server route. Without them it cannot be written,
only imagined, and imagining it is what the rest of this file refuses to do.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from .base import (
    DriverHealth,
    ForeignCamera,
    NotSupported,
    PtzCommand,
    StreamDescriptor,
    StreamProtocol,
    VmsDriver,
)

_REASON = (
    "Milestone XProtect federation is not implemented. It requires credentials "
    "for a live XProtect instance (Management Server address plus a service "
    "account with camera read rights) and a decision between the MIP SDK and the "
    "Mobile Server REST API. See the module docstring in federation/milestone.py."
)


class MilestoneDriver(VmsDriver):
    """Registered, contract-conformant, and refuses every call.

    Constructing it succeeds — that is intentional, so a misconfigured
    ``vms_platform`` value fails at the point of *use*, with a message naming the
    missing credentials, rather than at driver lookup with an opaque error about
    an unknown platform.
    """

    platform: ClassVar[str] = "milestone_xprotect"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)

    async def list_cameras(self) -> list[ForeignCamera]:
        raise NotSupported(_REASON)

    async def stream_url(
        self, vms_camera_id: str, *, protocol: StreamProtocol
    ) -> StreamDescriptor:
        raise NotSupported(_REASON)

    async def recording_url(
        self, vms_camera_id: str, start: datetime, end: datetime
    ) -> StreamDescriptor | None:
        # Note the return type permits None, which would read as "no recording in
        # that window". Returning it would be a lie of exactly the kind this stub
        # exists not to tell.
        raise NotSupported(_REASON)

    async def health(self, vms_camera_id: str) -> DriverHealth:
        # The base contract says health() returns rather than raises for ordinary
        # failures. This is not an ordinary failure: returning is_live=False would
        # write a row into app.camera_health_check asserting that a camera was
        # probed and found down, when nothing was probed at all. That row would
        # then be counted in uptime reports.
        raise NotSupported(_REASON)

    async def ptz(self, vms_camera_id: str, command: PtzCommand) -> None:
        raise NotSupported(_REASON)

    async def close(self) -> None:
        """Nothing was ever opened."""
        return None
