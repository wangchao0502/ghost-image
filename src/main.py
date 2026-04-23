from __future__ import annotations

import argparse
import logging
import sys
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from collections import Counter
from pathlib import Path

from weibo_album_crawler.browser import connect_via_cdp
from weibo_album_crawler.collector import (
    stream_records_api_first,
)
from weibo_album_crawler.downloader import (
    append_metadata,
    download_record,
    load_existing_record_ids,
    migrate_metadata_schema,
)
from weibo_album_crawler.logging_utils import setup_logger
from weibo_album_crawler.models import CrawlerConfig
from weibo_album_crawler.utils import ensure_dir, extract_blogger_id_from_url, normalize_image_quality


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Weibo album image crawler (CDP mode)")
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9222", help="CDP endpoint")
    parser.add_argument(
        "--album-url",
        default="https://weibo.com/u/1000000000",
        help="Target weibo profile/feed URL (API-first image hydration)",
    )
    parser.add_argument(
        "--blogger-id",
        default=None,
        help="Target blogger numeric id (e.g. 1000000000). Defaults to extracting from --album-url.",
    )
    parser.add_argument("--blogger-name", default="demo_blogger", help="Target blogger display name for metadata.")
    parser.add_argument("--dry-run", action="store_true", help="Collect metadata only")
    parser.add_argument("--max-items", type=int, default=None, help="Stop after N images")
    parser.add_argument("--max-rounds", type=int, default=150, help="Maximum scroll rounds")
    parser.add_argument("--stagnation-rounds", type=int, default=12, help="Stop after N stale rounds")
    parser.add_argument("--download-concurrency", type=int, default=3, help="Concurrent download workers")
    parser.add_argument(
        "--image-quality",
        default="large",
        help="Target Weibo image quality token (e.g. large, orj1080, mw690, orj360)",
    )
    parser.add_argument(
        "--images-dir",
        default="images",
        help="Image output root (metadata file will be created under this folder)",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Crawler log file path (default: <images-dir>/crawl.log)",
    )
    return parser


def print_summary(status_counter: Counter) -> None:
    print("\nRun summary")
    print("-" * 40)
    for key in ("downloaded", "metadata_only", "skipped_existing", "failed", "discovered"):
        if key in status_counter:
            print(f"{key:>16}: {status_counter[key]}")


def run() -> int:
    args = build_parser().parse_args()
    inferred_blogger_id = extract_blogger_id_from_url(args.album_url)
    blogger_id = (args.blogger_id or inferred_blogger_id or "").strip()
    if not blogger_id:
        raise ValueError("--blogger-id is required when it cannot be extracted from --album-url")

    images_dir = Path(args.images_dir).resolve()
    metadata_path = images_dir / "metadata.jsonl"
    ensure_dir(images_dir)
    log_file = Path(args.log_file).resolve() if args.log_file else (images_dir / "crawl.log")
    logger = setup_logger(log_file)
    logger.info("crawler start")
    logger.info("images_dir=%s metadata_path=%s log_file=%s", images_dir, metadata_path, log_file)
    image_quality = normalize_image_quality(args.image_quality)
    logger.info("blogger_id=%s blogger_name=%s image_quality=%s", blogger_id, args.blogger_name, image_quality)

    config = CrawlerConfig(
        cdp_url=args.cdp_url,
        album_url=args.album_url,
        blogger_id=blogger_id,
        blogger_name=(args.blogger_name or "").strip(),
        images_dir=images_dir,
        metadata_path=metadata_path,
        dry_run=args.dry_run,
        max_items=args.max_items,
        max_rounds=args.max_rounds,
        stagnation_rounds=args.stagnation_rounds,
        download_concurrency=max(1, min(6, args.download_concurrency)),
        image_quality=image_quality,
    )

    migrated_rows = migrate_metadata_schema(metadata_path, config.blogger_id, config.blogger_name)
    if migrated_rows:
        logger.info("metadata_schema_migrated_rows=%s", migrated_rows)
        print(f"Migrated {migrated_rows} metadata rows with blogger fields.")

    existing_ids = load_existing_record_ids(metadata_path, config.blogger_id)
    logger.info("loaded_existing_record_ids=%s", len(existing_ids))
    print(f"Loaded {len(existing_ids)} existing metadata records.")

    playwright, _browser, _context, page = connect_via_cdp(config.cdp_url)
    try:
        page.goto(config.album_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2200)
        logger.info("page ready url=%s", page.url)

        status_counter: Counter = Counter()
        pending: dict[Future, object] = {}
        discovered_total = 0

        def drain_completed(wait_all: bool = False) -> None:
            futures = list(pending.keys())
            if not futures:
                return
            if wait_all:
                selected = futures
            else:
                selected = [fut for fut in futures if fut.done()]

            for fut in selected:
                record = pending.pop(fut)
                try:
                    processed = fut.result()
                except Exception as exc:  # noqa: BLE001
                    processed = record
                    processed.status = "failed"
                    processed.error = str(exc)
                append_metadata([processed], metadata_path)
                status_counter[processed.status] += 1
                logger.info("download_result status=%s record_id=%s", processed.status, processed.record_id)

        with ThreadPoolExecutor(max_workers=config.download_concurrency) as pool:
            def on_records(records: list) -> bool:
                nonlocal discovered_total
                if not records:
                    return True
                # Persist discovered rows immediately before scheduling downloads.
                append_metadata(records, metadata_path)
                status_counter["discovered"] += len(records)
                discovered_total += len(records)
                logger.info("discovered_batch=%s discovered_total=%s", len(records), discovered_total)

                if config.dry_run:
                    for rec in records:
                        rec.status = "metadata_only"
                        append_metadata([rec], metadata_path)
                        status_counter["metadata_only"] += 1
                        logger.info("dry_run_record record_id=%s", rec.record_id)
                    return False if config.max_items and discovered_total >= config.max_items else True

                for rec in records:
                    future = pool.submit(download_record, rec, config)
                    pending[future] = rec

                drain_completed(wait_all=False)
                if config.max_items and discovered_total >= config.max_items:
                    return False
                return True

            emitted = stream_records_api_first(page, config, existing_ids, on_records, logger=logger)
            drain_completed(wait_all=True)

        if emitted == 0:
            logger.info("finished_no_new_images")
            print("No new images discovered.")
            return 0

        print_summary(status_counter)
        logger.info("summary=%s", dict(status_counter))

        failures = status_counter.get("failed", 0)
        if failures:
            print(f"\nFailures: {failures} (see metadata.jsonl error field for details)")
            logger.warning("failures=%s", failures)

        print(f"\nMetadata: {metadata_path.as_posix()}")
        print(f"Images root: {images_dir.as_posix()}")
        logger.info("crawler finished metadata=%s images_root=%s", metadata_path, images_dir)
        return 0
    finally:
        # Never close the user's real browser process when attached over CDP.
        playwright.stop()


if __name__ == "__main__":
    try:
        sys.exit(run())
    except Exception:  # noqa: BLE001
        logger = logging.getLogger("weibo_album_crawler")
        if logger.handlers:
            logger.error("fatal_exception\n%s", traceback.format_exc())
        raise

