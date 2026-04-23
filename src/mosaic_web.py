from __future__ import annotations

import uuid
from pathlib import Path

import cv2
import numpy as np
from flask import Flask, abort, jsonify, render_template, request, send_from_directory
from mosaic_cli import (
    ALLOWED_EXTENSIONS,
    build_mosaic,
    normalize_mosaic_params,
    normalize_quality_params,
)


BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
TEMPLATE_DIR = BASE_DIR / "mosaic_web" / "templates"
STATIC_DIR = BASE_DIR / "mosaic_web" / "static"
OUTPUT_DIR = PROJECT_ROOT / "datasets" / "mosaic_web_outputs"
TILES_DIR = OUTPUT_DIR / "tiles"
DEEPZOOM_TILE_SIZE = 256

app = Flask(__name__, template_folder=str(TEMPLATE_DIR), static_folder=str(STATIC_DIR))
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 1GB


def _is_allowed(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


def _read_image_from_upload(uploaded_file) -> np.ndarray | None:
    data = uploaded_file.read()
    if not data:
        return None
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    return image


def _resolve_tiles_dir(raw_path: str) -> Path:
    if not raw_path.strip():
        raise ValueError("Local tile directory path is empty.")

    candidate = Path(raw_path.strip())
    resolved = (PROJECT_ROOT / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()

    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise ValueError("Local tile directory must be inside project root.") from exc

    if not resolved.exists() or not resolved.is_dir():
        raise ValueError(f"Local tile directory not found: {resolved}")
    return resolved


def _load_tiles_from_dir(tiles_dir: Path) -> list[np.ndarray]:
    tile_images: list[np.ndarray] = []
    for image_path in sorted(tiles_dir.rglob("*")):
        if not image_path.is_file() or image_path.suffix.lower() not in ALLOWED_EXTENSIONS:
            continue
        tile_image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if tile_image is not None:
            tile_images.append(tile_image)
    return tile_images


def _build_tile_pyramid(image: np.ndarray, job_id: str, tile_size: int = DEEPZOOM_TILE_SIZE) -> dict:
    """
    Build deep-zoom tile pyramid for frontend viewport-based rendering.
    Levels are ordered from smallest(0) to largest(max).
    """
    levels_largest_to_smallest: list[np.ndarray] = []
    current = image
    while True:
        levels_largest_to_smallest.append(current)
        h, w = current.shape[:2]
        if max(w, h) <= tile_size:
            break
        next_w = max(1, w // 2)
        next_h = max(1, h // 2)
        current = cv2.resize(current, (next_w, next_h), interpolation=cv2.INTER_AREA)

    levels_small_to_large = list(reversed(levels_largest_to_smallest))
    full_h, full_w = image.shape[:2]
    job_dir = TILES_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    levels_meta: list[dict] = []
    for level_idx, level_img in enumerate(levels_small_to_large):
        level_h, level_w = level_img.shape[:2]
        scale = level_w / max(1, full_w)
        cols = int(np.ceil(level_w / tile_size))
        rows = int(np.ceil(level_h / tile_size))
        level_dir = job_dir / str(level_idx)
        level_dir.mkdir(parents=True, exist_ok=True)

        for y in range(rows):
            for x in range(cols):
                x0 = x * tile_size
                y0 = y * tile_size
                x1 = min(level_w, x0 + tile_size)
                y1 = min(level_h, y0 + tile_size)
                tile = level_img[y0:y1, x0:x1]
                tile_path = level_dir / f"{x}_{y}.png"
                cv2.imwrite(str(tile_path), tile, [int(cv2.IMWRITE_PNG_COMPRESSION), 2])

        levels_meta.append(
            {
                "level": level_idx,
                "width": level_w,
                "height": level_h,
                "cols": cols,
                "rows": rows,
                "scale": scale,
            }
        )

    return {
        "job_id": job_id,
        "tile_size": tile_size,
        "width": full_w,
        "height": full_h,
        "format": "png",
        "levels": levels_meta,
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/generate", methods=["POST"])
def generate():
    main_file = request.files.get("main_photo")
    tile_files = request.files.getlist("tile_photos")
    tile_dir = (request.form.get("tile_dir") or "").strip()

    if main_file is None or main_file.filename == "":
        return jsonify({"error": "Main photo is required."}), 400
    if not _is_allowed(main_file.filename):
        return jsonify({"error": "Unsupported main photo format."}), 400

    try:
        grid_cols = int(request.form.get("grid_cols", 180))
        tile_size = int(request.form.get("tile_size", 12))
        overlay_percent = int(request.form.get("overlay_percent", 12))
        diversity_strength = float(request.form.get("diversity_strength", 0.04))
        max_reuse = int(request.form.get("max_reuse", 4))
        sharpen_amount = float(request.form.get("sharpen_amount", 0.35))
        match_tile_size = int(request.form.get("match_tile_size", 16))
        preview_tile_size = int(request.form.get("preview_tile_size", 96))
    except ValueError:
        return jsonify({"error": "Mosaic settings have invalid number format."}), 400

    grid_cols, tile_size, overlay_percent = normalize_mosaic_params(
        grid_cols=grid_cols,
        tile_size=tile_size,
        overlay_percent=overlay_percent,
    )
    diversity_strength, max_reuse, sharpen_amount = normalize_quality_params(
        diversity_strength=diversity_strength,
        max_reuse=max_reuse,
        sharpen_amount=sharpen_amount,
    )
    match_tile_size = max(4, min(match_tile_size, 128))
    preview_tile_size = max(16, min(preview_tile_size, 256))

    main_image = _read_image_from_upload(main_file)
    if main_image is None:
        return jsonify({"error": "Failed to decode main photo."}), 400

    if tile_dir:
        try:
            resolved_tile_dir = _resolve_tiles_dir(tile_dir)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        tile_images = _load_tiles_from_dir(resolved_tile_dir)
    else:
        valid_tiles = [f for f in tile_files if f and f.filename and _is_allowed(f.filename)]
        if len(valid_tiles) < 5:
            return jsonify(
                {"error": "Please provide at least 5 tile photos, or fill local tile directory path."}
            ), 400

        tile_images = []
        for tile_file in valid_tiles:
            tile_img = _read_image_from_upload(tile_file)
            if tile_img is not None:
                tile_images.append(tile_img)

    if len(tile_images) < 5:
        return jsonify({"error": "Not enough valid tile photos after decoding (minimum 5)."}), 400

    try:
        mosaic = build_mosaic(
            main_image=main_image,
            tile_images=tile_images,
            grid_cols=grid_cols,
            tile_size=tile_size,
            overlay_percent=overlay_percent,
            diversity_strength=diversity_strength,
            max_reuse=max_reuse,
            sharpen_amount=sharpen_amount,
            match_tile_size=match_tile_size,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        return jsonify({"error": "Failed to generate mosaic image."}), 500

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_name = f"mosaic_{uuid.uuid4().hex[:12]}.jpg"
    output_path = OUTPUT_DIR / output_name
    cv2.imwrite(str(output_path), mosaic, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    preview_mosaic = mosaic
    if preview_tile_size > tile_size:
        try:
            preview_mosaic = build_mosaic(
                main_image=main_image,
                tile_images=tile_images,
                grid_cols=grid_cols,
                tile_size=preview_tile_size,
                overlay_percent=overlay_percent,
                diversity_strength=diversity_strength,
                max_reuse=max_reuse,
                sharpen_amount=sharpen_amount,
                match_tile_size=match_tile_size,
            )
        except Exception:
            preview_mosaic = mosaic

    tile_source = _build_tile_pyramid(preview_mosaic, job_id=output_name.rsplit(".", 1)[0])

    return jsonify(
        {
            "image_url": f"/result/{output_name}",
            "download_name": output_name,
            "tile_source": tile_source,
        }
    )


@app.route("/result/<path:filename>")
def result_file(filename: str):
    return send_from_directory(str(OUTPUT_DIR), filename, as_attachment=False)


@app.route("/tile/<job_id>/<int:level>/<int:x>/<int:y>.png")
def tile_file(job_id: str, level: int, x: int, y: int):
    if level < 0 or x < 0 or y < 0:
        abort(404)
    tile_path = TILES_DIR / job_id / str(level) / f"{x}_{y}.png"
    if not tile_path.exists() or not tile_path.is_file():
        abort(404)
    return send_from_directory(str(tile_path.parent), tile_path.name, as_attachment=False)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
