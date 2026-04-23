from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlretrieve

import cv2
import numpy as np
from ultralytics import YOLO


FINAL_STATUSES = {"downloaded", "skipped_existing"}


@dataclass(slots=True)
class Candidate:
    record_id: str
    local_path: Path
    raw: dict[str, Any]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Filter portraits and export centered square crops.")
    parser.add_argument("--metadata", default="images/metadata.jsonl", help="Input metadata jsonl path.")
    parser.add_argument("--datasets-root", default="datasets", help="Root output directory.")
    parser.add_argument("--process-code", required=True, help="Code used in run directory name.")
    parser.add_argument("--sample-size", type=int, default=20, help="How many images to process.")
    parser.add_argument(
        "--sample-mode",
        choices=("first", "random"),
        default="first",
        help="Sampling mode for candidate images.",
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed for random sample mode.")
    parser.add_argument("--device", default="0", help="YOLO device, e.g. 0, 1, cpu.")
    parser.add_argument("--batch-size", type=int, default=16, help="Batch size for YOLO person detection.")
    parser.add_argument("--yolo-imgsz", type=int, default=960, help="YOLO inference image size.")
    parser.add_argument("--person-conf-thres", type=float, default=0.25, help="Person confidence threshold.")
    parser.add_argument("--face-conf-thres", type=float, default=1.1, help="Face detector scale factor.")
    parser.add_argument("--face-min-neighbors", type=int, default=5, help="Face detector min neighbors.")
    parser.add_argument(
        "--face-detector-backend",
        choices=("auto", "yunet", "haar"),
        default="auto",
        help="Face detector backend. auto prefers YuNet and falls back to Haar.",
    )
    parser.add_argument(
        "--yunet-model-path",
        default="models/face_detection_yunet_2023mar.onnx",
        help="Path for YuNet ONNX model file.",
    )
    parser.add_argument("--yunet-score-thres", type=float, default=0.6, help="YuNet score threshold.")
    parser.add_argument("--out-size", type=int, default=300, help="Output image size.")
    parser.add_argument(
        "--person-scale",
        type=float,
        default=1.05,
        help="Square crop scale for single-person crops when no face is detected.",
    )
    parser.add_argument("--face-scale", type=float, default=1.8, help="Square crop scale for face bbox.")
    parser.add_argument(
        "--person-upperbody-ratio",
        type=float,
        default=0.22,
        help="When person exists but no face is detected, place crop center at person_top + ratio*person_height.",
    )
    parser.add_argument(
        "--center-tolerance",
        type=float,
        default=0.08,
        help="Maximum allowed normalized offset from image center.",
    )
    parser.add_argument("--write-workers", type=int, default=8, help="Workers for image write.")
    parser.add_argument(
        "--target-face-image",
        default="avator.jpg",
        help="Reference image path for identity filtering. Only photos matching this face are kept.",
    )
    parser.add_argument(
        "--face-match-thres",
        type=float,
        default=0.15,
        help="Similarity threshold for keeping target face photos (simple ~0.45, sface ~0.15).",
    )
    parser.add_argument(
        "--face-embedding-size",
        type=int,
        default=32,
        help="Embedding side length for grayscale face patch vectorization.",
    )
    parser.add_argument(
        "--face-identity-backend",
        choices=("simple", "sface"),
        default="simple",
        help="Identity backend for matching target face. simple=grayscale patch, sface=opencv model.",
    )
    parser.add_argument(
        "--sface-model-path",
        default="models/face_recognition_sface_2021dec.onnx",
        help="Path for SFace ONNX model file when --face-identity-backend sface.",
    )
    return parser


def load_candidates(metadata_path: Path) -> list[Candidate]:
    candidates: list[Candidate] = []
    with metadata_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            status = str(payload.get("status") or "").strip()
            if status not in FINAL_STATUSES:
                continue
            local_path = str(payload.get("local_path") or "").strip()
            if not local_path:
                continue
            path_obj = Path(local_path)
            if not path_obj.exists():
                continue
            candidates.append(
                Candidate(
                    record_id=str(payload.get("record_id") or ""),
                    local_path=path_obj,
                    raw=payload,
                )
            )
    return candidates


def sample_candidates(candidates: list[Candidate], sample_size: int, sample_mode: str, seed: int) -> list[Candidate]:
    if sample_size <= 0 or sample_size >= len(candidates):
        return candidates
    if sample_mode == "first":
        return candidates[:sample_size]
    rng = random.Random(seed)
    picked = rng.sample(candidates, sample_size)
    picked.sort(key=lambda item: item.local_path.as_posix())
    return picked


def person_bboxes(result: Any) -> list[list[float]]:
    boxes = result.boxes
    if boxes is None or len(boxes) == 0:
        return []
    xyxy = boxes.xyxy.cpu().numpy()
    conf = boxes.conf.cpu().numpy()
    areas = (xyxy[:, 2] - xyxy[:, 0]) * (xyxy[:, 3] - xyxy[:, 1])
    scores = areas * conf
    order = np.argsort(-scores)
    return [[float(v) for v in xyxy[int(idx)]] for idx in order]


def ensure_yunet_model(model_path: Path) -> bool:
    if model_path.exists() and model_path.stat().st_size > 1024:
        return True
    if model_path.exists() and model_path.stat().st_size <= 1024:
        model_path.unlink(missing_ok=True)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    urls = [
        "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        "https://cdn.jsdelivr.net/gh/opencv/opencv_zoo@main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        "https://raw.githubusercontent.com/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    ]
    for url in urls:
        try:
            urlretrieve(url, model_path.as_posix())
            if model_path.exists() and model_path.stat().st_size > 1024:
                return True
        except (URLError, OSError):
            continue
    return False


def ensure_sface_model(model_path: Path) -> bool:
    if model_path.exists() and model_path.stat().st_size > 1024:
        return True
    if model_path.exists() and model_path.stat().st_size <= 1024:
        model_path.unlink(missing_ok=True)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    urls = [
        "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
        "https://cdn.jsdelivr.net/gh/opencv/opencv_zoo@main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
        "https://raw.githubusercontent.com/opencv/opencv_zoo/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
    ]
    for url in urls:
        try:
            urlretrieve(url, model_path.as_posix())
            if model_path.exists() and model_path.stat().st_size > 1024:
                return True
        except (URLError, OSError):
            continue
    return False


def create_face_detector(args: argparse.Namespace) -> tuple[str, Any]:
    backend = args.face_detector_backend
    if backend in {"auto", "yunet"}:
        model_path = Path(args.yunet_model_path).resolve()
        if ensure_yunet_model(model_path):
            detector = cv2.FaceDetectorYN.create(
                model=model_path.as_posix(),
                config="",
                input_size=(320, 320),
                score_threshold=max(0.01, min(0.99, args.yunet_score_thres)),
                nms_threshold=0.3,
                top_k=5000,
            )
            if detector is not None:
                return "yunet", detector
        if backend == "yunet":
            raise RuntimeError(f"Failed to initialize YuNet from {model_path}")

    cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    face_detector = cv2.CascadeClassifier(cascade_path)
    if face_detector.empty():
        raise RuntimeError(f"Failed to load Haar cascade: {cascade_path}")
    return "haar", face_detector


def create_face_recognizer(args: argparse.Namespace) -> Any | None:
    if args.face_identity_backend != "sface":
        return None
    if not hasattr(cv2, "FaceRecognizerSF"):
        raise RuntimeError("OpenCV FaceRecognizerSF is not available in current cv2 build.")
    model_path = Path(args.sface_model_path).resolve()
    if not ensure_sface_model(model_path):
        raise RuntimeError(f"Failed to initialize SFace model from {model_path}")
    recognizer = cv2.FaceRecognizerSF.create(model_path.as_posix(), "")
    if recognizer is None:
        raise RuntimeError(f"Failed to create SFace recognizer from {model_path}")
    return recognizer


def detect_faces_bundle(
    image: np.ndarray,
    face_backend: str,
    face_detector: Any,
    scale_factor: float,
    min_neighbors: int,
) -> tuple[list[list[float]], list[np.ndarray] | None]:
    if face_backend == "yunet":
        h, w = image.shape[:2]
        face_detector.setInputSize((w, h))
        _, faces = face_detector.detect(image)
        if faces is None:
            return [], []
        out: list[list[float]] = []
        raw_rows: list[np.ndarray] = []
        for row in faces:
            x, y, fw, fh, score = float(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[14])
            out.append([x, y, x + fw, y + fh, score])
            raw_rows.append(np.asarray(row, dtype=np.float32))
        return out, raw_rows

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    faces = face_detector.detectMultiScale(
        gray,
        scaleFactor=max(1.01, scale_factor),
        minNeighbors=max(1, min_neighbors),
        minSize=(24, 24),
    )
    out = [[float(x), float(y), float(x + w), float(y + h), 1.0] for (x, y, w, h) in faces]
    return out, None


def detect_faces(
    image: np.ndarray,
    face_backend: str,
    face_detector: Any,
    scale_factor: float,
    min_neighbors: int,
) -> list[list[float]]:
    boxes, _ = detect_faces_bundle(
        image=image,
        face_backend=face_backend,
        face_detector=face_detector,
        scale_factor=scale_factor,
        min_neighbors=min_neighbors,
    )
    return boxes


def best_face_bbox(
    image: np.ndarray,
    face_backend: str,
    face_detector: Any,
    scale_factor: float,
    min_neighbors: int,
) -> list[float] | None:
    faces = detect_faces(
        image=image,
        face_backend=face_backend,
        face_detector=face_detector,
        scale_factor=scale_factor,
        min_neighbors=min_neighbors,
    )
    if not faces:
        return None
    scores = []
    for x1, y1, x2, y2, conf in faces:
        area = max(1.0, (x2 - x1) * (y2 - y1))
        scores.append(area * conf)
    idx = int(np.argmax(np.asarray(scores)))
    x1, y1, x2, y2, _ = faces[idx]
    return [x1, y1, x2, y2]


def count_faces(
    image: np.ndarray,
    face_backend: str,
    face_detector: Any,
    scale_factor: float,
    min_neighbors: int,
) -> int:
    return len(
        detect_faces(
            image=image,
            face_backend=face_backend,
            face_detector=face_detector,
            scale_factor=scale_factor,
            min_neighbors=min_neighbors,
        )
    )


def extract_face_embedding(image: np.ndarray, bbox: list[float], embedding_size: int) -> np.ndarray | None:
    height, width = image.shape[:2]
    x1, y1, x2, y2 = bbox[:4]
    ix1 = max(0, min(width - 1, int(np.floor(x1))))
    iy1 = max(0, min(height - 1, int(np.floor(y1))))
    ix2 = max(0, min(width, int(np.ceil(x2))))
    iy2 = max(0, min(height, int(np.ceil(y2))))
    if ix2 <= ix1 or iy2 <= iy1:
        return None

    patch = image[iy1:iy2, ix1:ix2]
    if patch.size == 0:
        return None

    if patch.ndim == 3 and patch.shape[2] == 3:
        patch = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
    patch = cv2.resize(
        patch,
        (max(8, embedding_size), max(8, embedding_size)),
        interpolation=cv2.INTER_AREA,
    )
    vec = patch.astype(np.float32).reshape(-1)
    vec -= float(vec.mean())
    norm = float(np.linalg.norm(vec))
    if norm <= 1e-6:
        vec = patch.astype(np.float32).reshape(-1)
        norm = float(np.linalg.norm(vec))
        if norm <= 1e-6:
            return None
    return vec / norm


def cosine_similarity(vec_a: np.ndarray, vec_b: np.ndarray) -> float:
    if vec_a.size == 0 or vec_b.size == 0 or vec_a.shape != vec_b.shape:
        return -1.0
    denom = float(np.linalg.norm(vec_a) * np.linalg.norm(vec_b))
    if denom <= 1e-6:
        return -1.0
    return float(np.dot(vec_a, vec_b) / denom)


def best_face_similarity(
    image: np.ndarray,
    face_boxes: list[list[float]],
    target_embedding: np.ndarray,
    embedding_size: int,
    identity_backend: str = "simple",
    face_recognizer: Any | None = None,
    face_raw_rows: list[np.ndarray] | None = None,
) -> tuple[float, list[float] | None]:
    best_score = -1.0
    best_bbox: list[float] | None = None
    for idx, box in enumerate(face_boxes):
        score = -1.0
        if identity_backend == "sface":
            if face_recognizer is None or not face_raw_rows or idx >= len(face_raw_rows):
                continue
            try:
                aligned = face_recognizer.alignCrop(image, face_raw_rows[idx])
                embedding = face_recognizer.feature(aligned)
                metric = getattr(cv2, "FaceRecognizerSF_FR_COSINE", 0)
                score = float(face_recognizer.match(target_embedding, embedding, metric))
            except cv2.error:
                continue
        else:
            emb = extract_face_embedding(image, box[:4], embedding_size=embedding_size)
            if emb is None:
                continue
            score = cosine_similarity(emb, target_embedding)
        if score > best_score:
            best_score = score
            best_bbox = [float(v) for v in box[:4]]
    return best_score, best_bbox


def centered_square_crop(
    image: np.ndarray,
    bbox: list[float],
    scale: float,
    out_size: int,
    center_override: tuple[float, float] | None = None,
) -> tuple[np.ndarray, list[float], list[float]]:
    height, width = image.shape[:2]
    x1, y1, x2, y2 = bbox
    cx = (x1 + x2) / 2.0 if center_override is None else center_override[0]
    cy = (y1 + y2) / 2.0 if center_override is None else center_override[1]
    bw = max(1.0, x2 - x1)
    bh = max(1.0, y2 - y1)
    side = max(2.0, min(max(bw, bh) * scale, float(min(width, height))))
    half = side / 2.0

    left = cx - half
    top = cy - half

    # Keep crop fully inside source image to guarantee no black borders.
    left = min(max(0.0, left), max(0.0, float(width) - side))
    top = min(max(0.0, top), max(0.0, float(height) - side))
    right = left + side
    bottom = top + side

    x0 = int(np.floor(left))
    y0 = int(np.floor(top))
    x1i = int(np.ceil(right))
    y1i = int(np.ceil(bottom))
    x1i = min(width, max(x0 + 1, x1i))
    y1i = min(height, max(y0 + 1, y1i))

    crop = image[y0:y1i, x0:x1i]

    resized = cv2.resize(crop, (out_size, out_size), interpolation=cv2.INTER_AREA)

    subject_x = (cx - left) / side
    subject_y = (cy - top) / side
    center_offset = [subject_x - 0.5, subject_y - 0.5]
    crop_bbox = [left, top, right, bottom]
    return resized, crop_bbox, center_offset


def ensure_run_dirs(datasets_root: Path, process_code: str) -> tuple[Path, Path, Path]:
    ts = datetime.now().strftime("%Y%m%d%H%M")
    run_dir = datasets_root / f"{process_code}_{ts}"
    images_dir = run_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=False)
    results_path = run_dir / "results.jsonl"
    return run_dir, images_dir, results_path


def output_name(candidate: Candidate, detector_type: str) -> str:
    stem = candidate.local_path.stem
    return f"{stem}__{detector_type}.jpg"


def write_image(path: Path, image: np.ndarray) -> bool:
    return bool(cv2.imwrite(path.as_posix(), image))


def dump_results(results_path: Path, records: list[dict[str, Any]]) -> None:
    with results_path.open("w", encoding="utf-8") as fh:
        for row in records:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> int:
    args = build_parser().parse_args()
    metadata_path = Path(args.metadata).resolve()
    if not metadata_path.exists():
        raise FileNotFoundError(f"metadata not found: {metadata_path}")

    datasets_root = Path(args.datasets_root).resolve()
    datasets_root.mkdir(parents=True, exist_ok=True)
    run_dir, images_dir, results_path = ensure_run_dirs(datasets_root, args.process_code)

    candidates = load_candidates(metadata_path)
    if not candidates:
        print("No valid candidates found in metadata.")
        return 0

    selected = sample_candidates(candidates, args.sample_size, args.sample_mode, args.seed)
    print(f"Candidates: {len(candidates)} | Selected: {len(selected)}")
    print(f"Run dir: {run_dir.as_posix()}")

    model = YOLO("yolov8n.pt")
    face_backend, face_detector = create_face_detector(args)
    face_recognizer = create_face_recognizer(args)
    print(f"Face detector backend: {face_backend}")
    print(f"Face identity backend: {args.face_identity_backend}")
    if args.face_identity_backend == "sface" and face_backend != "yunet":
        raise RuntimeError("SFace identity backend requires YuNet face detector backend.")
    target_face_path = Path(args.target_face_image).resolve()
    if not target_face_path.exists():
        raise FileNotFoundError(f"target face image not found: {target_face_path}")
    target_face_image = cv2.imread(target_face_path.as_posix())
    if target_face_image is None:
        raise RuntimeError(f"failed to read target face image: {target_face_path}")
    target_faces, target_face_rows = detect_faces_bundle(
        image=target_face_image,
        face_backend=face_backend,
        face_detector=face_detector,
        scale_factor=args.face_conf_thres,
        min_neighbors=args.face_min_neighbors,
    )
    if not target_faces:
        raise RuntimeError(f"no face detected in target face image: {target_face_path}")
    target_scores = []
    for idx, (x1, y1, x2, y2, conf) in enumerate(target_faces):
        area = max(1.0, (x2 - x1) * (y2 - y1))
        target_scores.append((area * conf, idx))
    target_idx = max(target_scores, key=lambda item: item[0])[1]
    target_face_bbox = target_faces[target_idx][:4]
    if args.face_identity_backend == "sface":
        if not target_face_rows or target_idx >= len(target_face_rows) or face_recognizer is None:
            raise RuntimeError("failed to prepare target face landmarks for SFace.")
        try:
            aligned_target = face_recognizer.alignCrop(target_face_image, target_face_rows[target_idx])
            target_embedding = face_recognizer.feature(aligned_target)
        except cv2.error as exc:
            raise RuntimeError(f"failed to build SFace embedding from target face image: {target_face_path}") from exc
    else:
        target_embedding = extract_face_embedding(
            target_face_image,
            target_face_bbox,
            embedding_size=max(8, args.face_embedding_size),
        )
    if target_embedding is None:
        raise RuntimeError(f"failed to build face embedding from target face image: {target_face_path}")
    print(f"Target face image: {target_face_path.as_posix()}")
    infer_batch_size = max(1, args.batch_size)

    status_counter: dict[str, int] = {}
    saved_person_count = 0
    saved_face_count = 0
    centered_violations = 0
    processed_count = 0

    def emit_row(fh: Any, row: dict[str, Any]) -> None:
        nonlocal processed_count, saved_person_count, saved_face_count
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        fh.flush()
        status = str(row.get("status") or "")
        status_counter[status] = status_counter.get(status, 0) + 1
        if status == "saved":
            if str(row.get("detector_type") or "") == "face":
                saved_face_count += 1
            else:
                saved_person_count += 1
        processed_count += 1
        if processed_count % 200 == 0:
            print(
                f"Progress: {processed_count}/{len(selected)} | saved={status_counter.get('saved', 0)} | "
                f"filtered_multi_person={status_counter.get('filtered_multi_person', 0)}"
            )

    with results_path.open("a", encoding="utf-8") as result_fh:
        for batch_start in range(0, len(selected), infer_batch_size):
            batch_candidates = selected[batch_start : batch_start + infer_batch_size]
            batch_results = model.predict(
                source=[item.local_path.as_posix() for item in batch_candidates],
                device=args.device,
                conf=args.person_conf_thres,
                classes=[0],
                batch=infer_batch_size,
                imgsz=max(320, args.yolo_imgsz),
                verbose=False,
            )

            for candidate, result in zip(batch_candidates, batch_results):
                person_boxes = person_bboxes(result)
                detector_type = "person"
                bbox = person_boxes[0] if person_boxes else None
                center_override: tuple[float, float] | None = None
                crop_scale_override: float | None = None

                img = result.orig_img
                if img is None:
                    emit_row(
                        result_fh,
                        {
                            "record_id": candidate.record_id,
                            "source_path": candidate.local_path.as_posix(),
                            "detector_type": "none",
                            "status": "image_read_failed",
                            "output_path": "",
                            "source_bbox": [],
                            "crop_bbox": [],
                            "center_offset": [0.0, 0.0],
                            "error": "empty image from detector",
                        },
                    )
                    continue

                if len(person_boxes) > 1:
                    emit_row(
                        result_fh,
                        {
                            "record_id": candidate.record_id,
                            "source_path": candidate.local_path.as_posix(),
                            "detector_type": "person",
                            "status": "filtered_multi_person",
                            "output_path": "",
                            "source_bbox": [],
                            "crop_bbox": [],
                            "center_offset": [0.0, 0.0],
                            "error": f"person_count={len(person_boxes)}",
                        },
                    )
                    continue

                all_faces, all_face_rows = detect_faces_bundle(
                    image=img,
                    face_backend=face_backend,
                    face_detector=face_detector,
                    scale_factor=args.face_conf_thres,
                    min_neighbors=args.face_min_neighbors,
                )
                face_match_score, matched_face_bbox = best_face_similarity(
                    image=img,
                    face_boxes=all_faces,
                    target_embedding=target_embedding,
                    embedding_size=max(8, args.face_embedding_size),
                    identity_backend=args.face_identity_backend,
                    face_recognizer=face_recognizer,
                    face_raw_rows=all_face_rows,
                )
                if matched_face_bbox is None:
                    emit_row(
                        result_fh,
                        {
                            "record_id": candidate.record_id,
                            "source_path": candidate.local_path.as_posix(),
                            "detector_type": "face",
                            "status": "filtered_identity_no_face",
                            "output_path": "",
                            "source_bbox": [],
                            "crop_bbox": [],
                            "center_offset": [0.0, 0.0],
                            "error": "no_face_for_identity_match",
                            "face_similarity": round(face_match_score, 6),
                        },
                    )
                    continue
                if face_match_score < args.face_match_thres:
                    emit_row(
                        result_fh,
                        {
                            "record_id": candidate.record_id,
                            "source_path": candidate.local_path.as_posix(),
                            "detector_type": "face",
                            "status": "filtered_non_target_face",
                            "output_path": "",
                            "source_bbox": [],
                            "crop_bbox": [],
                            "center_offset": [0.0, 0.0],
                            "error": f"face_similarity={face_match_score:.6f} < thres={args.face_match_thres:.3f}",
                            "face_similarity": round(face_match_score, 6),
                        },
                    )
                    continue

                if bbox is None:
                    detector_type = "face"
                    face_count = len(all_faces)
                    if face_count > 1:
                        emit_row(
                            result_fh,
                            {
                                "record_id": candidate.record_id,
                                "source_path": candidate.local_path.as_posix(),
                                "detector_type": "face",
                                "status": "filtered_multi_face",
                                "output_path": "",
                                "source_bbox": [],
                                "crop_bbox": [],
                                "center_offset": [0.0, 0.0],
                                "error": f"face_count={face_count}",
                            },
                        )
                        continue
                    bbox = matched_face_bbox
                else:
                    face_hint = matched_face_bbox
                    px1, py1, px2, py2 = bbox
                    person_cx = (px1 + px2) / 2.0
                    person_h = max(1.0, py2 - py1)
                    if face_hint is not None:
                        fx1, fy1, fx2, fy2 = face_hint
                        face_cx = (fx1 + fx2) / 2.0
                        face_cy = (fy1 + fy2) / 2.0
                        center_override = (face_cx, face_cy)
                        crop_scale_override = 1.00
                    else:
                        ratio = min(0.9, max(0.0, args.person_upperbody_ratio))
                        center_override = (person_cx, py1 + ratio * person_h)
                        crop_scale_override = args.person_scale

                if bbox is None:
                    emit_row(
                        result_fh,
                        {
                            "record_id": candidate.record_id,
                            "source_path": candidate.local_path.as_posix(),
                            "detector_type": "none",
                            "status": "no_person_or_face",
                            "output_path": "",
                            "source_bbox": [],
                            "crop_bbox": [],
                            "center_offset": [0.0, 0.0],
                            "error": "",
                        },
                    )
                    continue

                crop_scale = (
                    crop_scale_override
                    if (detector_type == "person" and crop_scale_override is not None)
                    else (args.person_scale if detector_type == "person" else args.face_scale)
                )
                cropped, crop_bbox, center_offset = centered_square_crop(
                    image=img,
                    bbox=bbox,
                    scale=crop_scale,
                    out_size=args.out_size,
                    center_override=center_override,
                )
                if abs(center_offset[0]) > args.center_tolerance or abs(center_offset[1]) > args.center_tolerance:
                    centered_violations += 1

                out_name = output_name(candidate, detector_type)
                out_path = images_dir / out_name
                write_ok = write_image(out_path, cropped)
                row = {
                    "record_id": candidate.record_id,
                    "source_path": candidate.local_path.as_posix(),
                    "detector_type": detector_type,
                    "status": "saved" if write_ok else "write_failed",
                    "output_path": out_path.as_posix() if write_ok else "",
                    "source_bbox": [round(v, 3) for v in bbox],
                    "crop_bbox": [round(v, 3) for v in crop_bbox],
                    "center_offset": [round(center_offset[0], 6), round(center_offset[1], 6)],
                    "face_similarity": round(face_match_score, 6),
                    "error": "" if write_ok else "cv2.imwrite failed",
                }
                emit_row(result_fh, row)

    saved_count = status_counter.get("saved", 0)
    person_count = saved_person_count
    face_count = saved_face_count
    print(f"Saved: {saved_count} (person={person_count}, face_fallback={face_count})")
    print(f"No target: {status_counter.get('no_person_or_face', 0)}")
    print(f"Filtered multi-person: {status_counter.get('filtered_multi_person', 0)}")
    print(f"Filtered multi-face: {status_counter.get('filtered_multi_face', 0)}")
    print(f"Filtered identity(no face): {status_counter.get('filtered_identity_no_face', 0)}")
    print(f"Filtered identity(non-target): {status_counter.get('filtered_non_target_face', 0)}")
    print(f"Center violations (> {args.center_tolerance}): {centered_violations}")
    print(f"Results: {results_path.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
