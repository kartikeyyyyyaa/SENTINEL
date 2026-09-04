"""Sentinel edge analytics worker.

Consumes RTSP, emits ``Sighting`` and ``PrimitiveEvent`` objects from
``services.common.events``. Nothing in this package writes to a camera, to a VMS,
or to the sandbox gateway, and no continuous video crosses the edge boundary.

Four modules carry the design:

* ``cascade`` — the compute funnel and, more importantly, the *measured* counters
  that justify it. Those numbers are a submission deliverable.
* ``source`` — every documented failure mode of the hackathon camera grid, one
  mitigation each, each with a comment saying which failure it answers.
* ``tracker`` — identity across frames, which is where a plausible-looking bug
  turns two vehicles into one that teleported.
* ``primitives`` — pure geometry over tracks. New incident types are authored as
  rules over these, not as new models.

Nothing here imports a model at module load. ``ultralytics``, ``torch`` and
``paddleocr`` are all lazy, behind a protocol with a deterministic stub
alternative, so the whole package imports, self-tests and runs its dry run on a
machine with no GPU, no weights and no route to the sandbox. That is not a
convenience: the development machines for this project have none of those things,
and a module-level ``import torch`` would make this service untestable on them.

Two more modules complete the pipeline. ``main`` is the process entrypoint: it
owns the per-camera threads, the shared (locked) detector and OCR reader, and the
loop that turns each source frame into a cascade result plus a primitive-engine
result and hands both to the sink. ``sink`` is where metadata leaves the edge —
bounded, batched, JSONL to stdout by default, HTTP to the registry when
configured. Nothing upstream of ``sink`` knows how events are delivered, and
``sink`` knows nothing about cameras, models or geometry.

Run it with, from the repository root::

    python -m services.analytics.main --self-test   # no network, no weights
    python -m services.analytics.main --dry-run      # synthetic frames, stub models
    python -m services.analytics.main                # config from environment

Note on layout: ``services/analytics/worker/`` is an earlier draft of this
package that predates ``services/common/events.py``. It carries its own parallel
event vocabulary with pixel-coordinate boxes and string camera ids, which no
longer matches the shared contract. This package is the one wired to
``services.common.events``; the older subpackage is superseded, imports nothing
from here, and nothing here imports from it. It is not deleted only because that
is a judgement call for whoever owns the repository, not for whoever is editing
one module in it — but nothing in this package or in ``main`` depends on it, and
it is safe to remove.
"""
from __future__ import annotations

__all__ = [
    "cascade",
    "clock",
    "config",
    "main",
    "primitives",
    "sink",
    "source",
    "stages",
    "tracker",
]

__version__ = "0.3.0"
