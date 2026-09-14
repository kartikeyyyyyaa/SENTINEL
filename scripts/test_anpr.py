#!/usr/bin/env python3
"""Standalone ANPR/detection tester — see the model work on ANY video.

The grid being down does not block testing: point this at a local file (a
traffic clip, a UCF-Crime RoadAccidents clip, a phone video of a car) or any
URL (HLS/RTSP), and it runs the same class of models the Sentinel worker uses
— YOLOv8n for detection, PaddleOCR for plate reading — draws the boxes and the
plate text on the video, prints every read as JSON, and writes an annotated
output clip you can drop straight into a demo. No registry, no docker, no grid.

    python scripts/test_anpr.py test.mp4
    python scripts/test_anpr.py test.mp4 --out demo.mp4 --show
    python scripts/test_anpr.py "https://cctv.corp8.cloud/cam04/index.m3u8"
    python scripts/test_anpr.py "rtsp://you%40mail.com:PASS@103.250.160.189:8554/stream/cam04"

Dependencies are the ones the analytics worker already pins:
    pip install ultralytics paddleocr paddlepaddle opencv-python
CPU is fine (slower). First run downloads the YOLOv8n weights automatically.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

# COCO ids for classes that carry a number plate on an Indian road, plus person.
PLATED = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}
PERSON = 0
PLATE_RE = re.compile(r"[^A-Z0-9]")


def _die(msg: str, code: int = 2) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def load_models(conf: float):
    try:
        import cv2  # noqa: F401
    except ImportError:
        _die("Missing OpenCV. Install: pip install opencv-python")
    try:
        from ultralytics import YOLO
    except ImportError:
        _die("Missing ultralytics. Install: pip install ultralytics")
    try:
        from paddleocr import PaddleOCR
    except ImportError:
        _die("Missing PaddleOCR. Install: pip install paddleocr paddlepaddle")

    print("loading YOLOv8n (first run downloads weights)…", file=sys.stderr)
    yolo = YOLO("yolov8n.pt")
    print("loading PaddleOCR…", file=sys.stderr)
    try:
        ocr = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
    except TypeError:  # newer paddleocr dropped show_log
        ocr = PaddleOCR(use_angle_cls=True, lang="en")
    return yolo, ocr


def read_plate(ocr, crop):
    """Return (text, score) for the most plate-like string in the crop, or None."""
    try:
        result = ocr.ocr(crop)
    except Exception:
        return None
    best = None
    # Handle both the 2.x nested-list shape and defensive fallbacks.
    for block in result or []:
        for line in block or []:
            try:
                text, score = line[1][0], float(line[1][1])
            except (TypeError, IndexError, ValueError):
                continue
            clean = PLATE_RE.sub("", text.upper())
            if 6 <= len(clean) <= 12 and any(c.isalpha() for c in clean) and any(c.isdigit() for c in clean):
                if best is None or score > best[1]:
                    best = (clean, score)
    return best


def main(argv=None) -> int:
    import cv2

    ap = argparse.ArgumentParser(description="Visual ANPR/detection tester")
    ap.add_argument("source", help="video file path, or URL (file/http/https/rtsp)")
    ap.add_argument("--out", default="anpr_demo.mp4", help="annotated output video path")
    ap.add_argument("--skip", type=int, default=2, help="process every Nth frame (speed)")
    ap.add_argument("--conf", type=float, default=0.35, help="detection confidence threshold")
    ap.add_argument("--max-seconds", type=float, default=0.0, help="stop after N seconds (0=all)")
    ap.add_argument("--show", action="store_true", help="show a live window (needs a display)")
    args = ap.parse_args(argv)

    src = args.source
    if src.lower().startswith("rtsp"):
        # Same rule the worker enforces: force TCP, UDP corrupts frames on NAT.
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"

    yolo, ocr = load_models(args.conf)

    cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        _die(
            "Could not open source: " + src + "\n"
            "  - local file? use an absolute path\n"
            "  - RTSP blocked (port 8554)? try the HLS URL instead\n"
            "  - check the URL plays in:  ffplay <source>"
        )

    in_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1280
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 720
    writer = cv2.VideoWriter(args.out, cv2.VideoWriter_fourcc(*"mp4v"),
                             max(1.0, in_fps / max(1, args.skip)), (w, h))

    seen = {}          # plate -> last frame index, to avoid spamming duplicates
    plates, persons_max, fi, processed, t0 = set(), 0, 0, 0, time.time()

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        fi += 1
        if args.max_seconds and (fi / in_fps) > args.max_seconds:
            break
        if fi % max(1, args.skip):
            continue
        processed += 1
        t_sec = round(fi / in_fps, 2)

        res = yolo(frame, verbose=False, conf=args.conf)[0]
        persons_this = 0
        for box in res.boxes:
            cls = int(box.cls[0]); cf = float(box.conf[0])
            x1, y1, x2, y2 = (int(v) for v in box.xyxy[0])
            if cls == PERSON:
                persons_this += 1
                cv2.rectangle(frame, (x1, y1), (x2, y2), (255, 170, 0), 1)
                continue
            if cls not in PLATED:
                continue
            cv2.rectangle(frame, (x1, y1), (x2, y2), (80, 200, 90), 2)
            label = PLATED[cls]
            # Plate prior: low and central on the vehicle (matches plate.py).
            ph = y2 - y1
            cy1 = y1 + int(ph * 0.55)
            crop = frame[cy1:y2, x1:x2]
            if crop.size:
                hit = read_plate(ocr, crop)
                if hit:
                    plate, score = hit
                    label = plate + f" ({score:.2f})"
                    if plate not in seen or fi - seen[plate] > 30:
                        seen[plate] = fi
                        plates.add(plate)
                        print(json.dumps({"frame": fi, "t_sec": t_sec, "class": PLATED[cls],
                                          "conf": round(cf, 3), "plate_text": plate,
                                          "ocr_conf": round(score, 3)}))
            cv2.putText(frame, label, (x1, max(0, y1 - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (80, 220, 100), 2)

        persons_max = max(persons_max, persons_this)
        if persons_this:
            cv2.putText(frame, f"persons: {persons_this}", (10, 26),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 170, 0), 2)

        writer.write(frame)
        if args.show:
            cv2.imshow("Sentinel ANPR test", frame)
            if cv2.waitKey(1) & 0xFF == 27:  # Esc
                break

    cap.release(); writer.release()
    if args.show:
        cv2.destroyAllWindows()

    dt = time.time() - t0
    print(json.dumps({
        "summary": True,
        "frames_processed": processed,
        "unique_plates": sorted(plates),
        "unique_plate_count": len(plates),
        "max_persons_in_frame": persons_max,
        "annotated_output": os.path.abspath(args.out),
        "seconds": round(dt, 1),
    }, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
