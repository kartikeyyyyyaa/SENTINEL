"""Driver lookup by platform name.

The table below is an explicit dict of **class objects**. There is no
``importlib.import_module(platform)`` anywhere in this package, and that omission
is a security control, not a stylistic preference.

``vms_platform`` is a ``text`` column in ``app.camera``. It is populated by CSV
bulk import and by the API, so its value is, in the worst case, attacker-chosen —
a bulk import is exactly the kind of privilege a mid-level user gets. Resolving a
driver by dynamically importing whatever string is in that column would turn "can
upload a camera CSV" into "can name any importable module in the process, and
execute its import-time side effects". Under a name like
``app.federation.registry`` that would look entirely reasonable in review.

So the mapping is closed. A platform this dict does not name cannot be
instantiated, whatever the database says. Extension happens by adding a line here
alongside a new module — a change that goes through code review, which is the
point.
"""
from __future__ import annotations

from typing import Any

from .base import FederationError, VmsDriver
from .genetec import GenetecDriver
from .milestone import MilestoneDriver
from .onvif_rtsp import OnvifRtspDriver
from .sentinel_grid import SentinelGridDriver

_DRIVERS: dict[str, type[VmsDriver]] = {
    SentinelGridDriver.platform: SentinelGridDriver,
    OnvifRtspDriver.platform: OnvifRtspDriver,
    MilestoneDriver.platform: MilestoneDriver,
    GenetecDriver.platform: GenetecDriver,
}

# Keys are derived from each class's own `platform` attribute rather than written
# out as literals, so the registry key and the value stored in
# app.camera.vms_platform cannot drift apart in a copy-paste. Belt and braces:
for _key, _cls in _DRIVERS.items():
    if _cls.platform != _key:  # pragma: no cover - structural assertion
        raise RuntimeError(f"driver {_cls.__name__} registered under {_key!r}")
    if not _key:  # pragma: no cover - structural assertion
        raise RuntimeError(f"driver {_cls.__name__} has an empty platform string")


def available_platforms() -> tuple[str, ...]:
    """Every platform name ``get_driver`` will accept, sorted.

    Sorted rather than insertion-ordered because this feeds an API response and a
    ``CHECK``-style validation message, and a stable order keeps both diffable.
    """
    return tuple(sorted(_DRIVERS))


def get_driver(platform: str, config: dict[str, Any]) -> VmsDriver:
    """Construct the driver for ``platform``.

    The name is trimmed and lowercased before lookup: ``vms_platform`` is free
    text that reaches us from spreadsheets, so ``"Sentinel_Grid "`` is a data
    entry artefact rather than a different platform. Nothing beyond case and
    whitespace is normalised — silently mapping ``"genetec"`` onto
    ``"genetec_security_center"`` would be guessing at intent.

    Raises ``FederationError`` naming the available platforms, because the person
    reading this error is usually an operator who mistyped a column in a CSV and
    needs the list of valid values, not a stack trace.
    """
    if not isinstance(platform, str) or not platform.strip():
        raise FederationError(
            f"no VMS platform given; expected one of {list(available_platforms())}"
        )
    key = platform.strip().lower()
    driver_cls = _DRIVERS.get(key)
    if driver_cls is None:
        raise FederationError(
            f"unknown VMS platform {platform!r}; available platforms are "
            f"{list(available_platforms())}"
        )
    return driver_cls(config)


def register_driver(driver_cls: type[VmsDriver]) -> None:
    """Add a driver at runtime. Takes a **class object**, never a module path.

    This exists for tests and for an operator-specific driver loaded by a
    deployment's own entry point. It keeps the security property intact — the
    caller must already hold the class, so nothing here converts a string from the
    database into imported code.

    Re-registering an existing platform raises rather than silently replacing it:
    a duplicate platform string means two modules disagree about who owns a
    vendor, and resolving that by last-import-wins is how a driver stops being
    called for reasons nobody can find.
    """
    key = driver_cls.platform.strip().lower()
    if not key:
        raise FederationError(f"{driver_cls.__name__} declares no platform string")
    if key in _DRIVERS and _DRIVERS[key] is not driver_cls:
        raise FederationError(
            f"platform {key!r} is already registered to {_DRIVERS[key].__name__}"
        )
    _DRIVERS[key] = driver_cls
