"""Model 3: the VMS federation layer.

One contract (``base.VmsDriver``), one closed lookup table (``registry``), and a
driver per platform. Adding a department's VMS is a module and a table entry;
nothing else in the platform changes. That is the whole architectural claim.

**Metadata only. Video never flows through this layer** — drivers return URLs and
the consumer connects to them directly. ``base`` explains, with the arithmetic,
why that boundary is not negotiable at 80,000 cameras.

Nothing in this package reads settings, the database or the environment. Drivers
are constructed from a plain config dict supplied by the caller, which is also
where credential decryption happens, so the decision about who may use a camera's
password stays in one auditable place instead of in every adapter.
"""
from __future__ import annotations

from .base import (
    CAMERA_TYPES,
    PROBES,
    AuthenticationFailed,
    DriverHealth,
    FederationError,
    ForeignCamera,
    NotSupported,
    PtzCommand,
    StreamDescriptor,
    StreamProtocol,
    UpstreamUnavailable,
    VmsDriver,
    normalise_camera_type,
    normalise_codec,
)
from .registry import available_platforms, get_driver, register_driver

__all__ = [
    "CAMERA_TYPES",
    "PROBES",
    "AuthenticationFailed",
    "DriverHealth",
    "FederationError",
    "ForeignCamera",
    "NotSupported",
    "PtzCommand",
    "StreamDescriptor",
    "StreamProtocol",
    "UpstreamUnavailable",
    "VmsDriver",
    "available_platforms",
    "get_driver",
    "normalise_camera_type",
    "normalise_codec",
    "register_driver",
]
