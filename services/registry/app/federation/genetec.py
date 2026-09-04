"""Genetec Security Center — documented stub.

Security Center turns up in Indian metro deployments, typically at city
surveillance or airport scale, and typically as the incumbent that a state
platform has to integrate with rather than replace. So it gets a driver slot.

**Everything here raises ``NotSupported``.** Same reasoning as
``milestone.py``: fabricated camera lists and invented stream URLs would survive
every demo and fail on the first real site, at which point the failure looks like a
platform defect rather than an unimplemented integration. Refusing is the only
behaviour that keeps "which platforms actually work" answerable.

The value of this file is that it demonstrates the adapter framework is **open for
extension**: a new platform is one module against one contract plus one entry in
``registry.py``, with no change to the database, the API, the console or any other
driver. Model 3's deliverable is that boundary, not a vendor count.

The real integration path
-------------------------

Security Center's integration surface is split, and both halves are needed:

1. **Web SDK** — an HTTP/REST façade over the Security Center SDK, hosted by the
   Web SDK role on the Directory server. This is the metadata and control plane:
   entity enumeration (cameras are ``Camera`` entities with GUID identifiers),
   entity properties, events, and PTZ. Queries are issued as text commands over
   HTTP and returned as XML or JSON depending on the requested format. GUIDs, not
   names, are the stable identifier — a mapping the ``vms_camera_id`` column
   already accommodates.

2. **Media Gateway** — the role that republishes Security Center video as standard
   RTSP (and, in current versions, HLS/WebRTC). This is what makes Genetec
   federable *without* violating the no-central-video rule, but only if the Media
   Gateway is deployed at the department alongside its Archiver. Pointing every
   Gujarat operator at one central Media Gateway would recreate the 160 Gbps
   problem described in ``base.py``.

Version-dependent specifics — the Web SDK's base path, the exact query syntax, the
Media Gateway's RTSP port and URL shape — are deliberately not asserted here.
They vary by Security Center version and by how the roles were deployed, and a
plausible-looking constant in this file would be indistinguishable from a verified
one. They get filled in against the actual deployment.

Authentication
--------------

Three things are required together, which is the part that surprises people:

* **HTTP Basic** over TLS, carrying a Security Center username and password. The
  credential encodes the target application as well as the user, so it is not
  simply ``user:pass``.
* **An SDK Certificate** — Genetec licences integrations per-integration. Without a
  valid certificate the Web SDK role will authenticate the user and then refuse
  the connection. This is a commercial prerequisite, not a technical one, and it
  has procurement lead time.
* **Privileges on the specific entities**, granted in Config Tool. A user who can
  log in can still enumerate nothing.

Which means the blocker is not code
------------------------------------

The blocker is **a live Security Center Directory, a Web SDK role that is actually
running, an SDK certificate, and a service account with entity privileges**. With
those, this driver is a short job. Without them it can only be guessed at, and
guessing is what the rest of this file declines to do.
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
    "Genetec Security Center federation is not implemented. It requires "
    "credentials for a live instance (Directory address, a Web SDK service "
    "account, and a Genetec SDK certificate) plus a departmental Media Gateway "
    "for RTSP re-streaming. See the module docstring in federation/genetec.py."
)


class GenetecDriver(VmsDriver):
    """Registered, contract-conformant, and refuses every call.

    Construction succeeds so that a camera row carrying
    ``vms_platform = 'genetec_security_center'`` produces a precise error at the
    point of use — naming the missing certificate and account — instead of an
    unknown-platform error at lookup time that tells the operator nothing about
    what to go and obtain.
    """

    platform: ClassVar[str] = "genetec_security_center"

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
        raise NotSupported(_REASON)

    async def health(self, vms_camera_id: str) -> DriverHealth:
        # Not is_live=False: that would write an app.camera_health_check row
        # claiming a probe happened. No probe happened.
        raise NotSupported(_REASON)

    async def ptz(self, vms_camera_id: str, command: PtzCommand) -> None:
        raise NotSupported(_REASON)

    async def close(self) -> None:
        """Nothing was ever opened."""
        return None
