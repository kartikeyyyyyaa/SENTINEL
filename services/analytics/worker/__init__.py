"""Sentinel edge analytics worker.

Consumes RTSP, emits metadata. Never the other way round: nothing in this package
writes to a camera or to the sandbox gateway, and no continuous video crosses the
edge boundary.

The four modules that carry the design are ``cascade`` (the compute funnel and its
measured counters), ``capture`` (the RTSP and PTS rules), ``primitives`` (the event
vocabulary the incident rules are authored over) and ``sink`` (bounded, lossy-by-
design egress). Nothing here imports a model at module load, so the package is
importable and testable on a machine with no GPU and no weights.
"""
from __future__ import annotations

__all__ = [
    "capture",
    "cascade",
    "config",
    "primitives",
    "sink",
    "stages",
    "tracker",
]

__version__ = "0.1.0"
