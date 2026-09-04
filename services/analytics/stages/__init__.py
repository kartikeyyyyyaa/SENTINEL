"""Cascade stages, in funnel order: motion, detect, plate, ocr.

Each stage is a class with per-stage state and its own drop counters, deliberately
not a function. Two of the four are stateful in ways that matter — the motion gate
holds a reference frame, the plate reader holds a per-track dedupe cache — and both
of those states must be resettable on a scene change. A functional interface would
have to pass that state in and out on every call, which is how a caller ends up
forgetting to reset one of them at a loop point and the worker starts attributing
one scene's plate reads to the next scene's vehicles.

Stage numbering matches the cost ordering, not the pipeline's history: stage 0 is
the one that runs on every frame and must be nearly free.
"""
from __future__ import annotations

from .detect import (
    COCO_TO_SENTINEL,
    Detection,
    Detector,
    StubDetector,
    UltralyticsDetector,
    build_detector,
    crop,
    letterbox,
    unletterbox,
)
from .motion import MotionDecision, MotionGate, variance_of_laplacian
from .plate import (
    OcrReader,
    PaddleOcrReader,
    PlateCrop,
    PlateLocaliser,
    PlateRead,
    PlateReadStage,
    StubOcrReader,
    build_reader,
    normalise,
)

__all__ = [
    "COCO_TO_SENTINEL",
    "Detection",
    "Detector",
    "MotionDecision",
    "MotionGate",
    "OcrReader",
    "PaddleOcrReader",
    "PlateCrop",
    "PlateLocaliser",
    "PlateRead",
    "PlateReadStage",
    "StubDetector",
    "StubOcrReader",
    "UltralyticsDetector",
    "build_detector",
    "build_reader",
    "crop",
    "letterbox",
    "normalise",
    "unletterbox",
    "variance_of_laplacian",
]
