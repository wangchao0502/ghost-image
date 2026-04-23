from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from weibo_album_crawler.downloader import download_record
from weibo_album_crawler.logging_utils import setup_logger
from weibo_album_crawler.models import CrawlerConfig, ImageRecord
from weibo_album_crawler.utils import month_bucket


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Repair metadata: redownload missing local files, then purge skipped_existing rows."
    )
    parser.add_argument(
        "--metadata",
        default="images/metadata.jsonl",
        help="Path to metadata.jsonl",
    )
    parser.add_argument(
        "--images-dir",
        default="images",
        help="Image root directory used for redownloaded files",
    )
    parser.add_argument(
        "--backup-dir",
        default="images/backups",
        help="Directory for metadata backup files before purge",
    )
    parser.add_argument(
        "--log-file",
        default="images/repair_metadata.log",
        help="Log file path",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=35.0,
        help="HTTP timeout for download retries",
    )
    return parser


def parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def payload_to_record(payload: dict[str, Any]) -> ImageRecord | None:
    record_id = str(payload.get("record_id") or "").strip()
    post_id = str(payload.get("post_id") or "").strip()
    post_url = str(payload.get("post_url") or "").strip()
    image_url = str(payload.get("image_url") or "").strip()
    if not (record_id and post_id and post_url and image_url):
        return None

    published_at = parse_dt(payload.get("published_at"))
    crawl_time = parse_dt(payload.get("crawl_time")) or datetime.now()
    published_month = str(payload.get("published_month") or "").strip() or month_bucket(published_at)

    return ImageRecord(
        record_id=record_id,
        blogger_id=str(payload.get("blogger_id") or "").strip(),
        blogger_name=str(payload.get("blogger_name") or "").strip(),
        post_id=post_id,
        post_url=post_url,
        image_url=image_url,
        published_at=published_at,
        published_month=published_month,
        weibo_text_raw=str(payload.get("weibo_text_raw") or ""),
        weibo_text_normalized=str(payload.get("weibo_text_normalized") or ""),
        local_path=str(payload.get("local_path") or ""),
        crawl_time=crawl_time,
        status=str(payload.get("status") or "discovered"),
        error=str(payload.get("error") or ""),
    )


def record_to_payload(record: ImageRecord) -> dict[str, Any]:
    payload = asdict(record)
    payload["published_at"] = record.published_at.isoformat() if record.published_at else None
    payload["crawl_time"] = record.crawl_time.isoformat()
    return payload


def should_redownload(payload: dict[str, Any]) -> bool:
    image_url = str(payload.get("image_url") or "").strip()
    if not image_url:
        return False
    local_path = str(payload.get("local_path") or "").strip()
    if not local_path:
        return True
    return not Path(local_path).exists()


def build_download_config(images_dir: Path, request_timeout: float) -> CrawlerConfig:
    return CrawlerConfig(
        cdp_url="",
        album_url="",
        blogger_id="",
        blogger_name="",
        images_dir=images_dir,
        metadata_path=images_dir / "metadata.jsonl",
        dry_run=False,
        max_items=None,
        max_rounds=1,
        stagnation_rounds=1,
        download_concurrency=1,
        request_timeout=request_timeout,
        max_retries=3,
    )


def run() -> int:
    args = build_parser().parse_args()
    metadata_path = Path(args.metadata).resolve()
    images_dir = Path(args.images_dir).resolve()
    backup_dir = Path(args.backup_dir).resolve()
    log_file = Path(args.log_file).resolve()

    logger = setup_logger(log_file)
    logger.info("repair start")
    logger.info("metadata=%s images_dir=%s backup_dir=%s", metadata_path, images_dir, backup_dir)

    if not metadata_path.exists():
        logger.error("metadata file does not exist: %s", metadata_path)
        return 1

    raw_lines = metadata_path.read_text(encoding="utf-8").splitlines()
    if not raw_lines:
        logger.info("metadata is empty, nothing to repair")
        return 0

    payloads: list[dict[str, Any]] = []
    invalid_lines = 0
    for line in raw_lines:
        text = line.strip()
        if not text:
            continue
        try:
            payloads.append(json.loads(text))
        except json.JSONDecodeError:
            invalid_lines += 1
    if invalid_lines:
        logger.warning("ignored invalid json lines=%s", invalid_lines)

    config = build_download_config(images_dir=images_dir, request_timeout=args.request_timeout)
    redownload_targets = [p for p in payloads if should_redownload(p)]
    logger.info("records_total=%s redownload_targets=%s", len(payloads), len(redownload_targets))

    redownloaded = 0
    redownload_failed = 0
    for idx, payload in enumerate(redownload_targets, start=1):
        record = payload_to_record(payload)
        if record is None:
            redownload_failed += 1
            logger.warning("skip invalid record for redownload payload=%s", payload)
            continue
        processed = download_record(record, config)
        payload.update(record_to_payload(processed))
        if processed.status in {"downloaded", "skipped_existing"}:
            redownloaded += 1
        else:
            redownload_failed += 1
        if idx % 100 == 0 or idx == len(redownload_targets):
            logger.info(
                "redownload progress current=%s/%s success_or_exists=%s failed=%s",
                idx,
                len(redownload_targets),
                redownloaded,
                redownload_failed,
            )

    skipped_existing_count = sum(1 for p in payloads if str(p.get("status") or "") == "skipped_existing")
    kept_payloads = [p for p in payloads if str(p.get("status") or "") != "skipped_existing"]

    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{metadata_path.stem}.{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl.bak"
    shutil.copy2(metadata_path, backup_path)
    logger.info("backup created path=%s", backup_path)

    with metadata_path.open("w", encoding="utf-8") as fh:
        for payload in kept_payloads:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

    logger.info(
        "purge skipped_existing removed=%s kept=%s metadata=%s",
        skipped_existing_count,
        len(kept_payloads),
        metadata_path,
    )
    logger.info(
        "rollback command: Copy-Item -Path '%s' -Destination '%s' -Force",
        backup_path,
        metadata_path,
    )
    logger.info(
        "repair finished redownload_targets=%s success_or_exists=%s failed=%s removed_skipped_existing=%s",
        len(redownload_targets),
        redownloaded,
        redownload_failed,
        skipped_existing_count,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())

