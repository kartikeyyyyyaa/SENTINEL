"""Cascade stages and the interface they share.

One narrow contract — ``run(ctx, items) -> items`` — for all four stages, even
though the item *type* changes at every step: frames in, frames out of the gate,
detections out of the detector, crops out of the plate stage, reads out of OCR.
Keeping the signature uniform is what lets ``cascade.py`` time and count every
stage with one piece of instrumentation instead of four, and those counts are a
submission deliverable rather than debug output.

The type changing along the chain is also why disabling a stage is not uniformly
safe. A stage declares ``passthrough_when_disabled`` if its output type equals its
input type — true only for the motion gate, which either forwards a frame or
drops it. Disabling any other stage truncates the cascade there, because the
stages after it consume a type nothing is producing any more. ``build_stages``
enforces that rather than leaving it as a trap.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..capture import Frame


@dataclass
class StageContext:
    """Per-frame scratch space shared down the chain.

    Stages need things earlier stages know: the plate stage needs the frame's
    pixels to crop from, OCR needs track ids so it can read a plate once per
    vehicle rather than once per frame. Passing them through the item list would
    mean a union type at every boundary and a wider contract; a context object
    keeps the item list honestly typed as "whatever this stage produces".

    ``extras`` is deliberately untyped and deliberately per-frame. Nothing in it
    survives the frame, so a stage cannot accumulate hidden state here — state
    that has to persist belongs on the stage instance where ``reset()`` can clear
    it at a loop cut.
    """

    frame: Frame
    camera_id: str
    t: float
    segment_id: int
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def image(self) -> Any:
        return self.frame.image


class Stage(ABC):
    """One step of the cascade.

    Stages are stateful across frames (the motion gate holds a reference frame,
    OCR holds which tracks it has already read) but must hold nothing across a
    loop cut, which is what ``reset()`` is for. A stage that forgot to implement
    ``reset`` would compare the first frame of a new scene against the last frame
    of the old one, and report the entire cut as motion.
    """

    name: str = "stage"
    # True only when output type == input type, so the stage can be skipped
    # without breaking the chain. See the module docstring.
    passthrough_when_disabled: bool = False

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        # How many items this stage did *expensive* work on during the last run.
        # None means "all of them", which is true for three of the four stages.
        # Stage 4 is the exception: its once-per-track rule means it is frequently
        # handed crops it deliberately declines to read, and counting those as work
        # would overstate the headline "OCR ran on N% of frames" figure that the
        # submission's sizing section quotes. Since that figure is a deliverable,
        # the stage reports its real call count rather than letting the cascade
        # infer it. See cascade.StageStats.
        self.last_work_units: int | None = None

    @abstractmethod
    def run(self, ctx: StageContext, items: list[Any]) -> list[Any]:
        """Consume this stage's inputs, produce the next stage's inputs.

        Must not raise for recoverable trouble. A single corrupt frame — and
        corrupt frames arrive routinely at join, see ``capture`` rule 6 — has to
        cost one frame's output, not the camera's thread. Return an empty list.
        """

    def reset(self) -> None:
        """Discard state that does not survive a scene cut."""

    def stats(self) -> dict[str, Any]:
        """Stage-specific counters, merged into the cascade report."""
        return {}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}(name={self.name!r}, enabled={self.enabled})"


__all__ = ["Stage", "StageContext"]
