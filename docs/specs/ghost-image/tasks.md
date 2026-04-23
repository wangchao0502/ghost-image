# Tasks: Ghost Image

## 1) Spec and project bootstrap
- [x] Create spec docs under `docs/specs/ghost-image/`.
- [x] Initialize package structure under `src/weibo_album_crawler/`.
- [x] Add `requirements.txt` and `README.md`.

## 2) Environment and dependencies
- [x] Create `.venv`.
- [x] Install dependencies (`playwright`, `httpx`, `python-dateutil`, `tenacity`).
- [x] Document browser CDP startup command for Edge/Chrome.

## 3) Core crawler implementation
- [x] Implement CDP connection helper.
- [x] Implement human-like scroll loop.
- [x] Implement post/image extraction with robust selectors and fallbacks.
- [x] Parse and normalize publish time into `YYYY-MM`.
- [x] Build image-level records linked by `post_id`.

## 4) Download and metadata persistence
- [x] Implement deterministic filename generation.
- [x] Save images into `images/YYYY-MM/`.
- [x] Persist records to `images/metadata.jsonl` incrementally.
- [x] Skip duplicates and existing files safely.

## 5) CLI and validation flow
- [x] Add flags: `--cdp-url`, `--album-url`, `--dry-run`, `--max-items`, `--max-rounds`, `--download-concurrency`.
- [x] Print final summary and failure hints.
- [x] Validate with dry-run and small sample download.

## 6) Acceptance checklist
- [x] Dry-run generates metadata records containing weibo text + publish time.
- [x] Download mode stores files under month folders.
- [x] Metadata links each image to post URL and local path.
- [x] Re-run remains idempotent.

## 7) Repository hygiene and docs
- [x] Add root `.gitignore` to exclude runtime artifacts (including `images/` and `datasets/`).
- [x] Keep `README.md` aligned with current crawler and mosaic workflows.
- [x] Add a Chinese README (`README.zh-CN.md`) for local collaboration.

## 8) Image processing pipeline
- [x] Add metadata/file consistency repair flow (`src/repair_metadata.py`).
- [x] Add portrait filtering + centered crop export (`src/portrait_filter_crop.py`).
- [x] Export process-run outputs under `datasets/<process_code>_<timestamp>/`.
- [x] Document processing workflow and key options in README.

## 9) Photo mosaic generation (CLI + Web)
- [x] Add local CLI entrypoint for mosaic generation (`src/mosaic_cli.py`).
- [x] Add local Web entrypoint for mosaic generation (`src/mosaic_web.py`).
- [x] Share parameter normalization and rendering core between CLI/Web paths.
- [x] Document local Web usage and output location.
