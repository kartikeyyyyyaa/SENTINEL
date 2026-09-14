"""Test package for the edge analytics service.

These tests exercise the pure-Python decision logic of the pipeline — plate
normalisation, watchlist matching, and the rule engine that turns primitives
into alerts — with **no camera, no GPU and no model weights**, needing nothing
installed beyond Tier 1 of ``requirements.txt`` (``numpy``). That is the same
bar the service's ``--self-test`` already meets and the same one the
``requirements.txt`` note promises for this suite; see the run commands there.

**Why this file puts the repo root on ``sys.path``.** The modules under test
import *both* ``from services.common...`` (a sibling package, one level above
this service) *and* ``from .config...`` (relative, within this service). The
first only resolves when the repository root is the import top level; the
second only resolves when ``services.analytics`` is imported as a package.
Satisfying both at once means the top-level package root has to be the repo
root regardless of the directory the test command is run from. Computing that
from ``__file__`` here — rather than relying on the caller's CWD — is what lets
the suite run unchanged with either of the commands documented in
``requirements.txt``.
"""
from __future__ import annotations

import os
import sys

# services/analytics/tests/__init__.py -> ../../.. is the repository root.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
