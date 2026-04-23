from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def normalize_mosaic_params(grid_cols: int, tile_size: int, overlay_percent: int) -> tuple[int, int, int]:
    """Clamp runtime parameters to safe ranges shared by CLI and Web."""
    normalized_grid_cols = max(20, min(int(grid_cols), 200))
    normalized_tile_size = max(8, min(int(tile_size), 80))
    normalized_overlay_percent = max(0, min(int(overlay_percent), 80))
    return normalized_grid_cols, normalized_tile_size, normalized_overlay_percent


def normalize_quality_params(
    diversity_strength: float,
    max_reuse: int,
    sharpen_amount: float,
) -> tuple[float, int, float]:
    """Clamp quality-focused parameters shared by CLI and Web."""
    normalized_diversity_strength = max(0.0, min(float(diversity_strength), 0.3))
    normalized_max_reuse = max(0, min(int(max_reuse), 1000))
    normalized_sharpen_amount = max(0.0, min(float(sharpen_amount), 2.0))
    return normalized_diversity_strength, normalized_max_reuse, normalized_sharpen_amount


def _center_square(image: np.ndarray) -> np.ndarray:
    h, w = image.shape[:2]
    side = min(h, w)
    y0 = (h - side) // 2
    x0 = (w - side) // 2
    return image[y0 : y0 + side, x0 : x0 + side]


def _prepare_tiles(
    tile_images: list[np.ndarray],
    render_tile_size: int,
    match_tile_size: int | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    prepared_tiles: list[np.ndarray] = []
    average_colors: list[np.ndarray] = []
    effective_match_size = render_tile_size if match_tile_size is None else max(4, int(match_tile_size))

    for tile in tile_images:
        if tile is None or tile.size == 0:
            continue
        squared = _center_square(tile)
        render_resized = cv2.resize(
            squared,
            (render_tile_size, render_tile_size),
            interpolation=cv2.INTER_AREA,
        )
        match_resized = (
            render_resized
            if effective_match_size == render_tile_size
            else cv2.resize(squared, (effective_match_size, effective_match_size), interpolation=cv2.INTER_AREA)
        )
        prepared_tiles.append(render_resized)
        average_colors.append(match_resized.mean(axis=(0, 1)))

    if not prepared_tiles:
        raise ValueError("No valid tile images available.")

    tiles_array = np.stack(prepared_tiles, axis=0)
    colors_array = np.stack(average_colors, axis=0).astype(np.float32) / 255.0
    return tiles_array, colors_array


def _assign_tiles_with_balance(
    pixels: np.ndarray,
    tile_avg_colors: np.ndarray,
    diversity_strength: float,
    max_reuse: int,
) -> np.ndarray:
    """
    Greedy assignment with optional usage penalty / reuse cap.
    This increases tile variety while keeping color matching close.
    """
    pixel_count = pixels.shape[0]
    tile_count = tile_avg_colors.shape[0]
    selected = np.zeros(pixel_count, dtype=np.int32)
    usage_counts = np.zeros(tile_count, dtype=np.int32)
    current_reuse_limit = max_reuse if max_reuse > 0 else None

    for i in range(pixel_count):
        distances = ((tile_avg_colors - pixels[i]) ** 2).sum(axis=1)
        if diversity_strength > 0:
            distances = distances + (usage_counts.astype(np.float32) * diversity_strength)

        if current_reuse_limit is not None:
            blocked = usage_counts >= current_reuse_limit
            if np.all(blocked):
                # Relax cap progressively when all tiles are exhausted.
                current_reuse_limit += 1
                blocked = usage_counts >= current_reuse_limit
            distances = np.where(blocked, np.inf, distances)

        idx = int(np.argmin(distances))
        selected[i] = idx
        usage_counts[idx] += 1

    return selected


def _unsharp_mask(image: np.ndarray, amount: float) -> np.ndarray:
    if amount <= 0:
        return image
    blurred = cv2.GaussianBlur(image, (0, 0), sigmaX=1.1, sigmaY=1.1)
    return cv2.addWeighted(image, 1.0 + amount, blurred, -amount, 0)


def build_mosaic(
    main_image: np.ndarray,
    tile_images: list[np.ndarray],
    grid_cols: int,
    tile_size: int,
    overlay_percent: int,
    diversity_strength: float = 0.0,
    max_reuse: int = 0,
    sharpen_amount: float = 0.0,
    match_tile_size: int | None = None,
) -> np.ndarray:
    tiles, tile_avg_colors = _prepare_tiles(
        tile_images=tile_images,
        render_tile_size=tile_size,
        match_tile_size=match_tile_size,
    )
    diversity_strength, max_reuse, sharpen_amount = normalize_quality_params(
        diversity_strength=diversity_strength,
        max_reuse=max_reuse,
        sharpen_amount=sharpen_amount,
    )

    main_h, main_w = main_image.shape[:2]
    grid_rows = max(1, int(round(grid_cols * (main_h / max(1, main_w)))))

    small_main = cv2.resize(main_image, (grid_cols, grid_rows), interpolation=cv2.INTER_AREA)
    pixels = small_main.reshape(-1, 3).astype(np.float32) / 255.0

    if diversity_strength <= 0 and max_reuse <= 0:
        distances = ((pixels[:, None, :] - tile_avg_colors[None, :, :]) ** 2).sum(axis=2)
        best_tile_indices = np.argmin(distances, axis=1).reshape(grid_rows, grid_cols)
    else:
        assigned = _assign_tiles_with_balance(
            pixels=pixels,
            tile_avg_colors=tile_avg_colors,
            diversity_strength=diversity_strength,
            max_reuse=max_reuse,
        )
        best_tile_indices = assigned.reshape(grid_rows, grid_cols)

    mosaic = np.zeros((grid_rows * tile_size, grid_cols * tile_size, 3), dtype=np.uint8)
    for row in range(grid_rows):
        for col in range(grid_cols):
            idx = int(best_tile_indices[row, col])
            y0 = row * tile_size
            x0 = col * tile_size
            mosaic[y0 : y0 + tile_size, x0 : x0 + tile_size] = tiles[idx]

    overlay_weight = max(0.0, min(overlay_percent / 100.0, 1.0))
    if overlay_weight > 0:
        resized_main = cv2.resize(
            main_image,
            (grid_cols * tile_size, grid_rows * tile_size),
            interpolation=cv2.INTER_AREA,
        )
        mosaic = cv2.addWeighted(mosaic, 1 - overlay_weight, resized_main, overlay_weight, 0)

    mosaic = _unsharp_mask(mosaic, sharpen_amount)
    return mosaic


def _load_image(path: Path) -> np.ndarray | None:
    if not path.exists() or not path.is_file():
        return None
    return cv2.imread(str(path), cv2.IMREAD_COLOR)


def _collect_tile_paths(tiles_dir: Path, recursive: bool) -> list[Path]:
    iterator = tiles_dir.rglob("*") if recursive else tiles_dir.glob("*")
    return [p for p in iterator if p.is_file() and p.suffix.lower() in ALLOWED_EXTENSIONS]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create photo mosaic from local files.")
    parser.add_argument("--main-image", required=True, help="Path to main image.")
    parser.add_argument("--tiles-dir", required=True, help="Directory containing tile images.")
    parser.add_argument("--output", required=True, help="Output image path, e.g. datasets/mosaic.jpg")
    parser.add_argument("--grid-cols", type=int, default=80, help="Mosaic grid column count (20-200).")
    parser.add_argument("--tile-size", type=int, default=20, help="Tile size in pixels (8-80).")
    parser.add_argument("--overlay-percent", type=int, default=20, help="Main image overlay percent (0-80).")
    parser.add_argument(
        "--diversity-strength",
        type=float,
        default=0.03,
        help="Tile diversity strength (0-0.3), higher uses more unique tiles.",
    )
    parser.add_argument(
        "--max-reuse",
        type=int,
        default=3,
        help="Preferred max reuse per tile, 0 means unlimited.",
    )
    parser.add_argument(
        "--sharpen-amount",
        type=float,
        default=0.35,
        help="Unsharp mask amount (0-2.0) for output clarity.",
    )
    parser.add_argument("--max-tiles", type=int, default=0, help="Limit tile count for fast testing, 0=all.")
    parser.add_argument("--recursive", action="store_true", help="Read tile images recursively.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    main_image_path = Path(args.main_image).resolve()
    tiles_dir = Path(args.tiles_dir).resolve()
    output_path = Path(args.output).resolve()

    if not main_image_path.exists():
        print(f"[ERROR] Main image not found: {main_image_path}")
        return 1
    if not tiles_dir.exists() or not tiles_dir.is_dir():
        print(f"[ERROR] Tiles directory not found: {tiles_dir}")
        return 1

    grid_cols, tile_size, overlay_percent = normalize_mosaic_params(
        grid_cols=args.grid_cols,
        tile_size=args.tile_size,
        overlay_percent=args.overlay_percent,
    )
    diversity_strength, max_reuse, sharpen_amount = normalize_quality_params(
        diversity_strength=args.diversity_strength,
        max_reuse=args.max_reuse,
        sharpen_amount=args.sharpen_amount,
    )

    main_image = _load_image(main_image_path)
    if main_image is None:
        print(f"[ERROR] Failed to decode main image: {main_image_path}")
        return 1

    tile_paths = _collect_tile_paths(tiles_dir, recursive=bool(args.recursive))
    if args.max_tiles > 0:
        tile_paths = tile_paths[: args.max_tiles]

    if len(tile_paths) < 5:
        print(f"[ERROR] Not enough tile images, found {len(tile_paths)} (minimum 5).")
        return 1

    tile_images: list[np.ndarray] = []
    for path in tile_paths:
        tile_image = _load_image(path)
        if tile_image is not None:
            tile_images.append(tile_image)

    if len(tile_images) < 5:
        print(f"[ERROR] Not enough decodable tile images, got {len(tile_images)} (minimum 5).")
        return 1

    print(
        f"[INFO] Generating mosaic with {len(tile_images)} tiles, "
        f"grid_cols={grid_cols}, tile_size={tile_size}, overlay={overlay_percent}%, "
        f"diversity={diversity_strength}, max_reuse={max_reuse}, sharpen={sharpen_amount}..."
    )
    mosaic = build_mosaic(
        main_image=main_image,
        tile_images=tile_images,
        grid_cols=grid_cols,
        tile_size=tile_size,
        overlay_percent=overlay_percent,
        diversity_strength=diversity_strength,
        max_reuse=max_reuse,
        sharpen_amount=sharpen_amount,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    ok = cv2.imwrite(str(output_path), mosaic, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    if not ok:
        print(f"[ERROR] Failed to write output image: {output_path}")
        return 1

    print(f"[DONE] Mosaic saved to: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
