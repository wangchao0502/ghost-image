# Proposal: Ghost Image

## Background
Ghost Image started as a crawler-focused project and has evolved into a local end-to-end image workflow:
- safe and repeatable download of images from a public profile page
- metadata cleanup and consistency repair
- portrait-oriented filtering/cropping for dataset preparation
- photo mosaic generation via both CLI and local Web UI

The crawler still targets the public profile page:
`https://weibo.com/u/1000000000?tabtype=album`.

The user session is already logged in on a local browser. The crawler must use this existing session and avoid all write actions.

## Goals
- Download all discoverable album images from the target account.
- Store images by post publish month in `images/YYYY-MM/`.
- Produce image-level metadata with:
  - associated weibo text
  - publish time
  - source post URL
  - local file path
- Support dry-run mode for metadata-only validation.
- Keep execution idempotent (skip existing files, avoid duplicate metadata).
- Provide deterministic post-processing flow from `images/metadata.jsonl` to `datasets/<run>/images`.
- Provide local mosaic generation through both CLI and Web entrypoints.

## Non-Goals
- Account login automation.
- Any write interaction on Weibo (like/follow/comment/private message).
- Bypassing anti-bot protections with evasive or high-risk behavior.
- Cloud deployment or hosted public service.

## Constraints
- Python implementation under `src/`.
- Runtime dependencies installed in local `.venv`.
- Browser connection via CDP to an already running, logged-in browser.
- Human-like pacing for scrolling and downloads.

## Risks and Mitigations
- **Account safety risk**: enforce read-only operations and selector allowlist.
- **Rate limiting**: randomized delays, bounded concurrency, cooldown windows.
- **Data quality drift**: keep raw and normalized text, preserve source references.
- **Partial failures**: retry with cap, persist progress and failure report.

## Acceptance Criteria
- Script can connect to local browser via CDP and open target album page.
- Dry-run outputs metadata without downloading files.
- Normal run downloads images to `images/YYYY-MM/`.
- Metadata file `images/metadata.jsonl` includes required fields per image.
- Re-run does not duplicate files or metadata records.
- Portrait filtering run exports cropped assets and run-level results under `datasets/`.
- Mosaic generation works from CLI and from local Web UI (`http://127.0.0.1:5000`).
