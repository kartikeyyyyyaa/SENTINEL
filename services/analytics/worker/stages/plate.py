"""Stage 3: crop candidate plate regions out of vehicle boxes.

Cheap, and its only job is to make stage 4 affordable. OCR on a full 1080p frame
is roughly two orders of magnitude more expensive than OCR on a 120x40 crop, so
throwing away everything that cannot be a plate is worth more than any accuracy
work inside stage 4.

**There is no plate-detection model here, on purpose.** PaddleOCR — and every
other serious OCR engine — already contains a text detector. Handing it a region
that certainly contains the plate and little else means its own detector does the
localisation for free. Adding a fourth model to ship, quantise, keep resident in
RAM and validate, in order to find a region we can locate with a geometric prior
plus a gradient scan, would cost real memory on every edge box for a marginal
gain. So stage 3 is arithmetic.

**The prior.** Plates sit low and horizontally central on the vehicle, occupy
roughly 35-55% of its width, and are wide relative to their height. That narrows a
vehicle box to a few percent of its area. Within that band, plate glyphs are dense
vertical strokes, so the sub-window with the highest horizontal-gradient energy is
usually the plate — a couple of numpy operations, no model, and it recovers the
cases where the prior alone is off by a body panel.

**No pixels leave in the primitive.** ``PlateCrop`` carries geometry only; the crop
array travels as a separate argument. A primitive that held a numpy view would keep
the entire decoded frame alive for as long as the event sat in the retry buffer,
which turns a bounded event buffer into an unbounded memory leak the first time the
registry is unreachable for a minute.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from ..config import PlateConfig
from ..primitives import Box, Detection, PlateCrop
from . import Stage, StageContext


class PlateCropStage(Stage):
    """Turns vehicle detections into plate-region crops.

    Persons are dropped here rather than earlier: they are needed by the tracker
    and by crowd density, and only stage 3 onwards has no use for them.
    """

    name = "plate"

    def __init__(self, config: PlateConfig, vehicle_labels: tuple[str, ...]) -> None:
        super().__init__(enabled=config.enabled)
        self.config = config
        self.vehicle_labels = set(vehicle_labels)
        self.rejected_too_small = 0
        self.rejected_not_vehicle = 0
        self.rejected_aspect = 0
        self.capped = 0

    def run(self, ctx: StageContext, items: list[Any]) -> list[Any]:
        detections: list[Detection] = [d for d in items if isinstance(d, Detection)]
        vehicles: list[Detection] = []
        for det in detections:
            if self.vehicle_labels and det.label not in self.vehicle_labels:
                self.rejected_not_vehicle += 1
                continue
            vehicles.append(det)

        # Largest box first. A larger box is a nearer vehicle, whose plate has more
        # pixels and is the one worth spending stage 4 on. Under the per-frame cap
        # this ordering means the crops we keep are the legible ones.
        vehicles.sort(key=lambda d: d.box.area, reverse=True)

        track_boxes: list[tuple[int, Box]] = ctx.extras.get("track_boxes", [])
        image = ctx.image
        crops: list[PlateCrop] = []
        arrays: list[np.ndarray] = []
        for det in vehicles:
            if len(crops) >= self.config.max_crops_per_frame:
                self.capped += 1
                break
            if min(det.box.width, det.box.height) < self.config.min_vehicle_box_px:
                self.rejected_too_small += 1
                continue
            region = self._candidate_box(det.box, image)
            if region is None:
                continue
            region = region.clip(float(ctx.frame.width), float(ctx.frame.height))
            if region.width < self.config.min_plate_width_px:
                self.rejected_too_small += 1
                continue
            aspect = region.aspect
            if not (self.config.aspect_min <= aspect <= self.config.aspect_max):
                self.rejected_aspect += 1
                continue
            crop_array = _extract(image, region)
            if crop_array is None or crop_array.size == 0:
                continue
            crops.append(
                PlateCrop(
                    box=region,
                    vehicle_box=det.box,
                    t=ctx.t,
                    track_id=_track_for(det.box, track_boxes),
                    score=float(min(1.0, region.area / max(1.0, det.box.area) * 20.0)),
                )
            )
            arrays.append(crop_array)

        # Pixels ride in the context, geometry rides in the item list. See the
        # module docstring for why the two are kept apart.
        ctx.extras["plate_crop_arrays"] = arrays
        return list(crops)

    def _candidate_box(self, vehicle: Box, image: Any) -> Box | None:
        """The geometric prior, optionally nudged by gradient energy."""
        cfg = self.config
        band_top = vehicle.y1 + vehicle.height * cfg.band_top
        band_bottom = vehicle.y1 + vehicle.height * cfg.band_bottom
        if band_bottom - band_top <= 1.0:
            return None

        width = vehicle.width * cfg.width_fraction
        height = width / cfg.target_aspect if cfg.target_aspect > 0 else vehicle.height * 0.2
        if height <= 1.0 or width <= 1.0:
            return None
        # Clamp the window to the band. A tall narrow vehicle box (a motorcycle
        # seen head-on) otherwise yields a window taller than the band it is
        # supposed to sit inside.
        height = min(height, band_bottom - band_top)

        cx = vehicle.center[0]
        # Default placement: horizontally centred, sitting on the bottom of the
        # band. Rear plates are near the bottom of the visible body, and a window
        # anchored to the band centre rides up onto the boot lid.
        y2 = band_bottom
        y1 = y2 - height
        x1, x2 = cx - width / 2.0, cx + width / 2.0

        if cfg.refine_by_edges and image is not None:
            refined = _refine_x_by_edges(
                image, band_top, band_bottom, vehicle.x1, vehicle.x2, width
            )
            if refined is not None:
                x1, x2 = refined

        pad_x = width * cfg.pad_fraction
        pad_y = height * cfg.pad_fraction
        return Box(x1=x1 - pad_x, y1=y1 - pad_y, x2=x2 + pad_x, y2=y2 + pad_y)


def _refine_x_by_edges(
    image: Any,
    band_top: float,
    band_bottom: float,
    x_left: float,
    x_right: float,
    window_width: float,
) -> tuple[float, float] | None:
    """Slide a window across the band and pick the highest gradient energy.

    Horizontal gradient only. Plate glyphs are vertical strokes, so they light up
    the x-derivative strongly; the horizontal body seams and shadow lines that
    dominate a vehicle's lower half light up the y-derivative instead. Using only
    the x-derivative is what makes this discriminate rather than just find "the
    busiest part of the bumper".

    Pure numpy, one cumulative sum, so the whole refinement is a few tens of
    microseconds on a crop this size — comfortably inside the budget for a stage
    that exists to save time.
    """
    try:
        y1, y2 = int(max(0, band_top)), int(min(image.shape[0], band_bottom))
        x1, x2 = int(max(0, x_left)), int(min(image.shape[1], x_right))
        if y2 - y1 < 4 or x2 - x1 < 8:
            return None
        band = image[y1:y2, x1:x2]
        gray = band.mean(axis=2) if band.ndim == 3 else band.astype(np.float32)
        energy = np.abs(np.diff(gray, axis=1)).sum(axis=0)  # Per-column x-gradient energy.
        win = int(max(4, min(window_width, energy.size)))
        if win >= energy.size:
            return float(x1), float(x1 + energy.size)
        # Sliding-window sums via a cumulative sum: O(n) rather than O(n*win).
        cumulative = np.concatenate(([0.0], np.cumsum(energy)))
        sums = cumulative[win:] - cumulative[:-win]
        best = int(np.argmax(sums))
        return float(x1 + best), float(x1 + best + win)
    except Exception:  # noqa: BLE001 - refinement is optional; the prior still stands
        return None


def _extract(image: Any, box: Box) -> np.ndarray | None:
    """Copy the crop out of the frame.

    ``.copy()`` and not a view. A view keeps the whole frame's buffer alive for as
    long as anything holds the crop, and stage 4 may hold a crop across a retry.
    """
    if image is None:
        return None
    x1, y1, x2, y2 = box.as_int()
    h, w = image.shape[:2]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    if x2 <= x1 or y2 <= y1:
        return None
    return np.array(image[y1:y2, x1:x2], copy=True)


def _track_for(box: Box, track_boxes: list[tuple[int, Box]], min_iou: float = 0.5) -> int | None:
    """Resolve which track a detection belongs to, by overlap.

    Resolved here by IOU rather than threaded through the item type from the
    tracker. Identity plumbing across four stages would widen every stage's
    contract to carry a field only stage 4 uses; the overlap test is a handful of
    comparisons over the live tracks and gets the same answer.
    """
    best_id: int | None = None
    best = min_iou
    for track_id, track_box in track_boxes:
        score = box.iou(track_box)
        if score >= best:
            best, best_id = score, track_id
    return best_id
