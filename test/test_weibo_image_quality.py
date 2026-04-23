from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if SRC_DIR.as_posix() not in sys.path:
    sys.path.insert(0, SRC_DIR.as_posix())

from weibo_album_crawler.browser import connect_via_cdp
from weibo_album_crawler.collector import stream_records_api_first
from weibo_album_crawler.models import CrawlerConfig
from weibo_album_crawler.utils import extract_blogger_id_from_url, normalize_image_quality


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare Weibo image URLs across quality tokens.")
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9222", help="CDP endpoint")
    parser.add_argument("--album-url", default="https://weibo.com/u/1000000001", help="Target weibo profile URL")
    parser.add_argument("--blogger-id", default=None, help="Optional explicit blogger id")
    parser.add_argument("--blogger-name", default="quality-test", help="Display name for in-memory records")
    parser.add_argument("--max-items", type=int, default=5, help="Collect N image URLs per quality")
    parser.add_argument(
        "--qualities",
        default="large,mw690,orj360,orj1080",
        help="Comma-separated quality tokens, e.g. large,mw690,orj360,orj1080",
    )
    return parser


def parse_qualities(raw: str) -> list[str]:
    values = [x.strip() for x in (raw or "").split(",") if x.strip()]
    if not values:
        raise ValueError("At least one quality token is required.")
    return [normalize_image_quality(v) for v in values]


def make_config(args: argparse.Namespace, quality: str) -> CrawlerConfig:
    blogger_id = (args.blogger_id or extract_blogger_id_from_url(args.album_url) or "").strip()
    if not blogger_id:
        raise ValueError("--blogger-id is required when it cannot be extracted from --album-url")
    base = Path("images") / "quality_compare" / quality
    return CrawlerConfig(
        cdp_url=args.cdp_url,
        album_url=args.album_url,
        blogger_id=blogger_id,
        blogger_name=args.blogger_name,
        images_dir=base,
        metadata_path=base / "metadata.jsonl",
        dry_run=True,
        max_items=args.max_items,
        max_rounds=40,
        stagnation_rounds=8,
        image_quality=quality,
    )


def run() -> int:
    args = build_parser().parse_args()
    qualities = parse_qualities(args.qualities)
    max_items = max(1, args.max_items)

    playwright, _browser, context, _page = connect_via_cdp(args.cdp_url)
    try:
        for quality in qualities:
            config = make_config(args, quality)
            collected: list[str] = []
            page = context.new_page()
            page.goto(config.album_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2200)

            def on_records(records: list) -> bool:
                for record in records:
                    if len(collected) >= max_items:
                        return False
                    collected.append(record.image_url)
                return len(collected) < max_items

            stream_records_api_first(page, config, existing_record_ids=set(), on_records=on_records, logger=None)
            page.close()

            print(f"\nquality={quality} collected={len(collected)}")
            for idx, url in enumerate(collected[:max_items], start=1):
                print(f"{idx:02d}. {url}")
    finally:
        playwright.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(run())
