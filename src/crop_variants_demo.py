from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlretrieve

import cv2
import numpy as np
from ultralytics import YOLO


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate multiple crop variants for one image.")
    parser.add_argument("--image", required=True, help="Source image path.")
    parser.add_argument("--datasets-root", default="datasets", help="Root output directory.")
    parser.add_argument("--process-code", default="variants", help="Run directory prefix.")
    parser.add_argument("--device", default="0", help="YOLO device.")
    parser.add_argument("--out-size", type=int, default=300, help="Output image size.")
    parser.add_argument(
        "--face-detector-backend",
        choices=("yunet", "haar"),
        default="yunet",
        help="Face detector backend for guided variants.",
    )
    parser.add_argument("--yunet-model-path", default="models/face_detection_yunet_2023mar.onnx")
    return parser


def best_person_bbox(result: Any) -> list[float] | None:
    boxes = result.boxes
    if boxes is None or len(boxes) == 0:
        return None
    xyxy = boxes.xyxy.cpu().numpy()
    conf = boxes.conf.cpu().numpy()
    areas = (xyxy[:, 2] - xyxy[:, 0]) * (xyxy[:, 3] - xyxy[:, 1])
    scores = areas * conf
    idx = int(np.argmax(scores))
    return [float(v) for v in xyxy[idx]]


def ensure_yunet_model(model_path: Path) -> bool:
    if model_path.exists() and model_path.stat().st_size > 1024:
        return True
    if model_path.exists() and model_path.stat().st_size <= 1024:
        model_path.unlink(missing_ok=True)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    urls = [
        "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        "https://cdn.jsdelivr.net/gh/opencv/opencv_zoo@main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    ]
    for url in urls:
        try:
            urlretrieve(url, model_path.as_posix())
            if model_path.exists() and model_path.stat().st_size > 1024:
                return True
        except (URLError, OSError):
            continue
    return False


def best_face_bbox(
    image: np.ndarray,
    backend: str,
    yunet_detector: Any | None,
    haar_detector: cv2.CascadeClassifier | None,
) -> list[float] | None:
    if backend == "yunet" and yunet_detector is not None:
        h, w = image.shape[:2]
        yunet_detector.setInputSize((w, h))
        _, faces = yunet_detector.detect(image)
        if faces is None:
            return None
        scores = []
        boxes: list[list[float]] = []
        for row in faces:
            x, y, fw, fh, score = float(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[14])
            boxes.append([x, y, x + fw, y + fh])
            scores.append(max(1.0, fw * fh) * score)
        idx = int(np.argmax(np.asarray(scores)))
        return boxes[idx]

    if haar_detector is None:
        return None
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    faces = haar_detector.detectMultiScale(gray, scaleFactor=1.05, minNeighbors=4, minSize=(24, 24))
    if len(faces) == 0:
        return None
    areas = [int(w * h) for (_, _, w, h) in faces]
    idx = int(np.argmax(areas))
    x, y, w, h = faces[idx]
    return [float(x), float(y), float(x + w), float(y + h)]


def crop_square(
    image: np.ndarray,
    person_bbox: list[float],
    out_size: int,
    scale: float,
    center_x: float,
    center_y: float,
) -> tuple[np.ndarray, list[float]]:
    h, w = image.shape[:2]
    px1, py1, px2, py2 = person_bbox
    pw = max(1.0, px2 - px1)
    ph = max(1.0, py2 - py1)
    side = max(2.0, min(max(pw, ph) * scale, float(min(w, h))))

    left = center_x - side / 2.0
    top = center_y - side / 2.0
    left = min(max(0.0, left), max(0.0, float(w) - side))
    top = min(max(0.0, top), max(0.0, float(h) - side))
    right = left + side
    bottom = top + side

    x0 = int(np.floor(left))
    y0 = int(np.floor(top))
    x1 = int(np.ceil(right))
    y1 = int(np.ceil(bottom))
    x1 = min(w, max(x0 + 1, x1))
    y1 = min(h, max(y0 + 1, y1))

    crop = image[y0:y1, x0:x1]
    out = cv2.resize(crop, (out_size, out_size), interpolation=cv2.INTER_AREA)
    return out, [left, top, right, bottom]


def main() -> int:
    args = build_parser().parse_args()
    image_path = Path(args.image).resolve()
    if not image_path.exists():
        raise FileNotFoundError(f"image not found: {image_path}")

    ts = datetime.now().strftime("%Y%m%d%H%M")
    run_dir = Path(args.datasets_root).resolve() / f"{args.process_code}_{image_path.stem}_{ts}"
    run_dir.mkdir(parents=True, exist_ok=False)

    model = YOLO("yolov8n.pt")
    result = model.predict(
        source=image_path.as_posix(),
        device=args.device,
        conf=0.25,
        classes=[0],
        verbose=False,
    )[0]
    image = result.orig_img
    if image is None:
        raise RuntimeError("failed to load image via detector")

    person_bbox = best_person_bbox(result)
    if person_bbox is None:
        raise RuntimeError("no person detected in source image")

    px1, py1, px2, py2 = person_bbox
    pw = px2 - px1
    ph = py2 - py1

    face_backend = args.face_detector_backend
    yunet_detector: Any | None = None
    haar_detector: cv2.CascadeClassifier | None = None
    if face_backend == "yunet":
        model_path = Path(args.yunet_model_path).resolve()
        if ensure_yunet_model(model_path):
            yunet_detector = cv2.FaceDetectorYN.create(
                model=model_path.as_posix(),
                config="",
                input_size=(320, 320),
                score_threshold=0.6,
                nms_threshold=0.3,
                top_k=5000,
            )
        if yunet_detector is None:
            face_backend = "haar"

    if face_backend == "haar":
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        haar_detector = cv2.CascadeClassifier(cascade_path)
        if haar_detector.empty():
            haar_detector = None

    face_bbox = best_face_bbox(
        image=image,
        backend=face_backend,
        yunet_detector=yunet_detector,
        haar_detector=haar_detector,
    )

    # Multiple strategies for side-by-side visual comparison.
    centers: list[tuple[str, float, float, float]] = [
        ("person_center_s115", 1.15, (px1 + px2) / 2.0, (py1 + py2) / 2.0),
        ("person_top30_s105", 1.05, (px1 + px2) / 2.0, py1 + 0.30 * ph),
        ("person_top25_s095", 0.95, (px1 + px2) / 2.0, py1 + 0.25 * ph),
    ]

    if face_bbox is not None:
        fx1, fy1, fx2, fy2 = face_bbox
        fcx, fcy = (fx1 + fx2) / 2.0, (fy1 + fy2) / 2.0
        centers.extend(
            [
                ("face_guided_075_s105", 1.05, (px1 + px2) / 2.0 * 0.25 + fcx * 0.75, (py1 + py2) / 2.0 * 0.25 + fcy * 0.75),
                ("face_guided_100_s100", 1.00, fcx, fcy),
            ]
        )
    else:
        centers.extend(
            [
                ("upperbody_hint_s105", 1.05, (px1 + px2) / 2.0, py1 + 0.22 * ph),
                ("upperbody_hint_s095", 0.95, (px1 + px2) / 2.0, py1 + 0.20 * ph),
            ]
        )

    summary: list[dict[str, Any]] = []
    for name, scale, cx, cy in centers:
        out_img, crop_bbox = crop_square(
            image=image,
            person_bbox=person_bbox,
            out_size=args.out_size,
            scale=scale,
            center_x=cx,
            center_y=cy,
        )
        out_path = run_dir / f"{name}.jpg"
        cv2.imwrite(out_path.as_posix(), out_img)
        summary.append(
            {
                "name": name,
                "output_path": out_path.as_posix(),
                "scale": scale,
                "center": [round(cx, 3), round(cy, 3)],
                "crop_bbox": [round(v, 3) for v in crop_bbox],
            }
        )

    # Also export the source with person bbox for reference.
    vis = image.copy()
    cv2.rectangle(vis, (int(px1), int(py1)), (int(px2), int(py2)), (0, 255, 0), 3)
    if face_bbox is not None:
        fx1, fy1, fx2, fy2 = face_bbox
        cv2.rectangle(vis, (int(fx1), int(fy1)), (int(fx2), int(fy2)), (255, 0, 0), 2)
    cv2.imwrite((run_dir / "source_with_boxes.jpg").as_posix(), vis)

    info = {
        "source_image": image_path.as_posix(),
        "face_backend": face_backend,
        "person_bbox": [round(v, 3) for v in person_bbox],
        "face_bbox": [round(v, 3) for v in face_bbox] if face_bbox is not None else None,
        "variants": summary,
    }
    (run_dir / "variants.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Variants saved to: {run_dir.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
