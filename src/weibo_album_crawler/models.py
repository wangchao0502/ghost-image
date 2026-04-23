from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass(slots=True)
class CrawlerConfig:
    cdp_url: str
    album_url: str
    blogger_id: str
    blogger_name: str
    images_dir: Path
    metadata_path: Path
    dry_run: bool = False
    max_items: Optional[int] = None
    max_rounds: int = 120
    stagnation_rounds: int = 8
    min_scroll_delay: float = 1.0
    max_scroll_delay: float = 2.8
    min_round_cooldown: float = 0.8
    max_round_cooldown: float = 2.2
    download_concurrency: int = 1
    request_timeout: float = 35.0
    max_retries: int = 3
    image_quality: str = "large"


@dataclass(slots=True)
class RawPost:
    post_id: str
    post_url: str
    publish_text: str
    publish_title: str
    weibo_text: str
    image_urls: list[str]


@dataclass(slots=True)
class ImageRecord:
    record_id: str
    blogger_id: str
    blogger_name: str
    post_id: str
    post_url: str
    image_url: str
    published_at: Optional[datetime]
    published_month: str
    weibo_text_raw: str
    weibo_text_normalized: str
    local_path: str
    crawl_time: datetime
    status: str
    error: str = ""

