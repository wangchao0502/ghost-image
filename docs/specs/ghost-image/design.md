# Design: Ghost Image

## Architecture
- `src/main.py`: crawler CLI entrypoint and run orchestration.
- `src/weibo_album_crawler/browser.py`: CDP connection and browser/page setup.
- `src/weibo_album_crawler/collector.py`: album traversal, metadata extraction, dedupe.
- `src/weibo_album_crawler/downloader.py`: image download, retries, archive routing.
- `src/weibo_album_crawler/models.py`: typed models and run config.
- `src/weibo_album_crawler/utils.py`: delays, time parsing, text normalization, hashing.
- `src/repair_metadata.py`: metadata/file consistency repair pass.
- `src/portrait_filter_crop.py`: person-first filtering + centered crop dataset export.
- `src/mosaic_cli.py`: shared mosaic core and CLI usage.
- `src/mosaic_web.py`: local Web UI for mosaic generation.

## Runtime Flow
### A) Crawler and metadata
1. Parse CLI options (CDP URL, album URL, dry-run, limits).
2. Connect to existing browser via CDP and select/create a page.
3. Navigate to album URL.
4. Repeatedly perform human-like scroll cycles with randomized intervals.
5. Extract post cards and image URLs from current DOM snapshot.
6. Parse publish time and normalize to `YYYY-MM`.
7. Build image-level records (one record per image, linked by `post_id`).
8. If dry-run, persist metadata only; else download with bounded retries.
9. Save metadata incrementally in `images/metadata.jsonl`.
10. Print run summary and failure list.

### B) Image processing / dataset flow
1. Read `images/metadata.jsonl` and keep records with valid local files.
2. Run person detection first; fallback to face detection as needed.
3. Apply centered crop strategy and export normalized outputs.
4. Write assets and run-level JSONL into `datasets/<process_code>_<timestamp>/`.

### C) Mosaic generation flow (CLI + Web)
1. Load main image and candidate tile library from local disk.
2. Normalize parameters (`grid_cols`, `tile_size`, `overlay_percent`, etc.).
3. Execute shared tile matching/rendering core from `mosaic_cli.py`.
4. Save output image (CLI path or Web output directory).

## Data Model
Each image record will include:
- `record_id`: deterministic hash of `post_id + image_url`.
- `post_id`
- `post_url`
- `image_url`
- `published_at` (ISO8601 when possible)
- `published_month` (`YYYY-MM`, fallback `unknown`)
- `weibo_text_raw`
- `weibo_text_normalized`
- `local_path` (empty in dry-run)
- `crawl_time` (ISO8601)
- `status` (`downloaded`, `skipped_existing`, `metadata_only`, `failed`)
- `error` (optional)

## Human-like and Safety Controls
- Randomized scroll step and pause range.
- Randomized cooldown between extraction rounds.
- Bounded max rounds and stagnation cutoff when no new records are found.
- Download concurrency default = 1, configurable up to low safe ceiling.
- No click actions except safe navigation to target URL.
- No form fills, keyboard submit, or action buttons.

## Idempotency Strategy
- Use deterministic `record_id`.
- Maintain in-memory set loaded from existing `metadata.jsonl` to skip duplicates.
- Skip download if target file already exists; still persist record status.

## Error Handling
- Retry downloads with exponential-ish randomized backoff and max attempts.
- Continue run on per-item failures.
- Persist failures in metadata with `status=failed` and `error`.

## Validation Strategy
- Dry-run with low max-items to validate extraction and parsing.
- Small download run to validate directory and filename strategy.
- Full run with summary metrics:
  - discovered records
  - downloaded
  - skipped existing
  - failed
- Processing run validation:
  - exported crop count
  - proportion of person/face-guided crops
  - output image dimensions and naming consistency
- Mosaic validation:
  - successful output generation from CLI and Web
  - parameter bounds and clamping behavior
