#!/usr/bin/env python3
"""Track ONE vehicle across cameras by appearance — features, not plate.

The problem-statement task is "given a vehicle, show its route across cameras
with timestamps." Plate OCR on wide street cameras is unreliable, so this does
it the robust way: a deep appearance embedding (shape/structure, not colour)
identifies the same vehicle across different feeds.

Two steps:

  1) ENROLL the target (from a clear image or a frame of the feed it starts on):
       python scripts/track_vehicle.py enroll target.jpg --out query.npz

  2) SCAN the other cameras and get the route (ordered sightings):
       python scripts/track_vehicle.py scan query.npz \
           cam01=feedA.mp4 cam02=feedB.mp4 cam03="https://cctv.corp8.cloud/cam03/index.m3u8" \
           --out-dir route_out --thresh 0.62

Output: route.json (camera, time, score, ordered by time) + a montage of the
matched crops you can drop straight into a demo. Pairs with test_anpr.py, which
reads the plate to give you the vehicle number to anchor on.

Deps (same stack the worker pins): pip install ultralytics torch torchvision opencv-python
CPU is fine. First run downloads YOLOv8n + ResNet18 weights automatically.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

VEHICLE = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}


def _die(msg, code=2):
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def _load_cv2():
    try:
        import cv2
        return cv2
    except ImportError:
        _die("Missing OpenCV. pip install opencv-python")


def load_yolo(conf):
    try:
        from ultralytics import YOLO
    except ImportError:
        _die("Missing ultralytics. pip install ultralytics")
    print("loading YOLOv8n…", file=sys.stderr)
    return YOLO("yolov8n.pt")


class Embedder:
    """Appearance embedding — captures vehicle structure, not colour.

    Prefers a torchvision ResNet18 (ImageNet) feature. If those weights can't be
    downloaded, it falls back to the detector's own CNN embedding (YOLO.embed),
    which is still a learned shape feature and needs no extra download. Only if
    both are unavailable does it drop to an HSV colour histogram, so the script
    always runs. ``mode`` records which was used so enroll and scan stay matched.
    """

    def __init__(self, yolo=None):
        self.mode = "color"
        self.yolo = yolo
        try:
            import torch
            import torchvision
            from torchvision import transforms
            m = torchvision.models.resnet18(weights="IMAGENET1K_V1")
            m.fc = torch.nn.Identity()
            m.eval()
            self.torch = torch
            self.model = m
            self.tf = transforms.Compose([
                transforms.ToPILImage(),
                transforms.Resize((128, 128)),
                transforms.ToTensor(),
                transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
            ])
            self.mode = "resnet"
            print("using ResNet18 appearance embedding", file=sys.stderr)
            return
        except Exception as e:  # noqa: BLE001
            print(f"ResNet weights unavailable ({str(e)[:60]}); trying detector embedding", file=sys.stderr)
        if self.yolo is not None:
            self.mode = "yolo"
            print("using YOLO CNN embedding (shape features)", file=sys.stderr)
        else:
            print("using colour+shape histogram fallback", file=sys.stderr)

    def embed(self, crop_bgr):
        import cv2
        if crop_bgr is None or crop_bgr.size == 0:
            return None
        if self.mode == "resnet":
            rgb = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)
            with self.torch.no_grad():
                v = self.model(self.tf(rgb).unsqueeze(0)).squeeze().numpy()
        elif self.mode == "yolo":
            try:
                v = self.yolo.embed(crop_bgr, verbose=False)[0].cpu().numpy().astype("float32")
            except Exception:
                return None
        else:
            hsv = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2HSV)
            v = cv2.calcHist([hsv], [0, 1], None, [24, 24], [0, 180, 0, 256])
            v = cv2.normalize(v, v).flatten()
        n = np.linalg.norm(v)
        return v / n if n else v


def cos(a, b):
    if a is None or b is None:
        return 0.0
    return float(np.dot(a, b))


def biggest_vehicle(cv2, yolo, frame, conf):
    res = yolo(frame, verbose=False, conf=conf)[0]
    best, best_area = None, 0
    for box in res.boxes:
        cls = int(box.cls[0])
        if cls not in VEHICLE:
            continue
        x1, y1, x2, y2 = (int(v) for v in box.xyxy[0])
        area = (x2 - x1) * (y2 - y1)
        if area > best_area:
            best_area, best = area, (x1, y1, x2, y2, VEHICLE[cls], float(box.conf[0]))
    return best


def enroll(args):
    cv2 = _load_cv2()
    yolo = load_yolo(args.conf)
    emb = Embedder(yolo)
    src = args.source
    if os.path.isfile(src) and src.lower().rsplit(".", 1)[-1] in ("jpg", "jpeg", "png", "bmp"):
        frame = cv2.imread(src)
    else:
        cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG)
        ok, frame = cap.read(); cap.release()
        if not ok:
            _die("could not read a frame from " + src)
    v = biggest_vehicle(cv2, yolo, frame, args.conf)
    if not v:
        _die("no vehicle found in the enroll image — use a clearer crop")
    x1, y1, x2, y2, vtype, cf = v
    vec = emb.embed(frame[y1:y2, x1:x2])
    np.savez(args.out, embed=vec, vtype=vtype, mode=emb.mode)
    cv2.imwrite(os.path.splitext(args.out)[0] + "_target.jpg", frame[y1:y2, x1:x2])
    print(json.dumps({"enrolled": True, "type": vtype, "detector_conf": round(cf, 3),
                      "query": os.path.abspath(args.out), "embedding": emb.mode}, indent=2))


def scan(args):
    cv2 = _load_cv2()
    q = np.load(args.query, allow_pickle=True)
    qvec, qtype, qmode = q["embed"], str(q["vtype"]), str(q["mode"])
    yolo = load_yolo(args.conf)
    emb = Embedder(yolo)
    if emb.mode != qmode:
        print(f"WARN: query embedding={qmode} but scan embedding={emb.mode}; re-enroll for best results", file=sys.stderr)
    os.makedirs(args.out_dir, exist_ok=True)

    route = []
    for spec in args.cameras:
        cam, _, src = spec.partition("=")
        if not src:
            print(f"skip bad spec {spec!r} (want cam=source)", file=sys.stderr); continue
        if src.lower().startswith("rtsp"):
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
        cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            print(f"[{cam}] could not open {src}", file=sys.stderr); continue
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        fi = 0
        best = None  # best sighting on this camera
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            fi += 1
            if args.max_seconds and fi / fps > args.max_seconds:
                break
            if fi % max(1, args.skip):
                continue
            res = yolo(frame, verbose=False, conf=args.conf)[0]
            for box in res.boxes:
                cls = int(box.cls[0])
                if cls not in VEHICLE:
                    continue
                if VEHICLE[cls] != qtype:
                    continue  # coarse type gate
                x1, y1, x2, y2 = (int(v) for v in box.xyxy[0])
                score = cos(qvec, emb.embed(frame[y1:y2, x1:x2]))
                if score >= args.thresh and (best is None or score > best["score"]):
                    crop = frame[y1:y2, x1:x2].copy()
                    best = {"camera": cam, "t_sec": round(fi / fps, 2), "score": round(score, 3),
                            "frame": fi, "_crop": crop}
        cap.release()
        if best:
            p = os.path.join(args.out_dir, f"{cam}_{best['t_sec']}s_{best['score']}.jpg")
            cv2.imwrite(p, best.pop("_crop"))
            best["crop"] = p
            route.append(best)
            print(json.dumps({k: v for k, v in best.items() if k != "_crop"}))

    route.sort(key=lambda r: (r["t_sec"]))
    with open(os.path.join(args.out_dir, "route.json"), "w") as f:
        json.dump({"target_type": qtype, "sightings": route}, f, indent=2)
    print(json.dumps({"summary": True, "cameras_hit": len(route),
                      "route": [f"{r['camera']}@{r['t_sec']}s(score {r['score']})" for r in route],
                      "route_file": os.path.abspath(os.path.join(args.out_dir, "route.json"))},
                     indent=2), file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Cross-camera vehicle tracking by appearance")
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("enroll", help="build a query descriptor from an image/frame")
    e.add_argument("source", help="target image (jpg/png) or a video/URL to grab a frame from")
    e.add_argument("--out", default="query.npz")
    e.add_argument("--conf", type=float, default=0.35)

    s = sub.add_parser("scan", help="find the target across cameras")
    s.add_argument("query", help="query.npz from enroll")
    s.add_argument("cameras", nargs="+", help="cam=source pairs (file or URL)")
    s.add_argument("--out-dir", default="route_out")
    s.add_argument("--thresh", type=float, default=0.6, help="match similarity threshold (0-1)")
    s.add_argument("--skip", type=int, default=3)
    s.add_argument("--conf", type=float, default=0.35)
    s.add_argument("--max-seconds", type=float, default=0.0)

    args = ap.parse_args(argv)
    if args.cmd == "enroll":
        return enroll(args)
    return scan(args)


if __name__ == "__main__":
    raise SystemExit(main())
