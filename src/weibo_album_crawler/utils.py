from __future__ import annotations

import hashlib
import os
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from dateutil import parser as date_parser

IMAGE_QUALITY_TOKEN_PATTERN = re.compile(
    r"/(thumb150|square|mw\d+|orj\d+|bmiddle|large|original|mw2000)/",
    re.IGNORECASE,
)


def sleep_jitter(min_seconds: float, max_seconds: float) -> None:
    """Sleep for a randomized duration to reduce bot-like rhythm."""
    if max_seconds <= 0:
        return
    span_min = max(0.0, min_seconds)
    span_max = max(span_min, max_seconds)
    time.sleep(random.uniform(span_min, span_max))


def normalize_text(value: str) -> str:
    compact = re.sub(r"\s+", " ", (value or "").strip())
    return compact


def sha1_short(value: str, length: int = 12) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:length]


def build_record_id(blogger_id: str, post_id: str, canonical_image_url: str) -> str:
    safe_blogger_id = (blogger_id or "").strip()
    safe_post_id = (post_id or "").strip()
    safe_url = (canonical_image_url or "").strip()
    return sha1_short(f"{safe_blogger_id}|{safe_post_id}|{safe_url}", length=16)


def extract_blogger_id_from_url(url: str) -> str:
    text = (url or "").strip()
    if not text:
        return ""
    match = re.search(r"weibo\.com/u/(\d+)", text) or re.search(r"weibo\.com/(\d+)(?:/|$)", text)
    return match.group(1) if match else ""


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def sanitize_filename(name: str) -> str:
    return re.sub(r"[<>:\"/\\|?*\x00-\x1F]", "_", name).strip(" .")


def extension_from_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.lower()
    ext = os.path.splitext(path)[1]
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
        return ext
    return ".jpg"


def normalize_image_quality(value: str) -> str:
    """
    Normalize desired Weibo image quality token.
    Supports common quality families such as:
    - large
    - orj1080 / orj360
    - mw690 / mw2000
    """
    text = (value or "").strip().lower()
    if text == "large":
        return "large"
    if re.fullmatch(r"orj\d+", text):
        return text
    if re.fullmatch(r"mw\d+", text):
        return text
    raise ValueError(
        f"Unsupported image quality: {value!r}. Use one of large/orj<width>/mw<width>, e.g. large, orj1080, mw690."
    )


def upgrade_image_url(url: str, image_quality: str = "large") -> str:
    """Rewrite sinaimg quality token to requested output quality."""
    normalized_quality = normalize_image_quality(image_quality)
    parsed = urlparse(url)
    if "sinaimg.cn" not in (parsed.netloc or "").lower():
        return url
    if IMAGE_QUALITY_TOKEN_PATTERN.search(url):
        return IMAGE_QUALITY_TOKEN_PATTERN.sub(f"/{normalized_quality}/", url, count=1)
    return url


def canonicalize_image_url(url: str) -> str:
    """Normalize URL for dedupe by removing volatile query params."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def is_content_image_url(url: str) -> bool:
    """
    Heuristic filter that keeps likely feed-content images
    and removes avatars/icons/decorative assets.
    """
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    full = f"{host}{path}"

    if "sinaimg.cn" not in host:
        return False

    blocked_host_prefixes = ("h5.sinaimg.cn", "d.sinaimg.cn")
    if host.startswith(blocked_host_prefixes):
        return False

    blocked_tokens = (
        "vip_top_default",
        "timeline_card_small_video_default",
        "feed_icon_",
        "avatar",
        "badge",
        "icon",
        "svip_",
        "/upload/",
    )
    if any(token in full for token in blocked_tokens):
        return False

    content_markers = ("/large/", "/orj480/", "/mw2000/", "/original/", "/crop.")
    if any(marker in path for marker in content_markers):
        return True

    return path.endswith((".jpg", ".jpeg", ".png", ".webp"))


def month_bucket(dt: datetime | None) -> str:
    if dt is None:
        return "unknown"
    return dt.strftime("%Y-%m")


def parse_weibo_time(raw_text: str, raw_title: str = "") -> datetime | None:
    """Parse common Weibo time strings into local datetime."""
    now = datetime.now()
    text = normalize_text(raw_title or raw_text)
    if not text:
        return None

    if text == "刚刚":
        return now

    minute_match = re.match(r"(\d+)\s*分钟前", text)
    if minute_match:
        return now - timedelta(minutes=int(minute_match.group(1)))

    hour_match = re.match(r"(\d+)\s*小时前", text)
    if hour_match:
        return now - timedelta(hours=int(hour_match.group(1)))

    today_match = re.match(r"今天\s*(\d{1,2}):(\d{2})", text)
    if today_match:
        return now.replace(
            hour=int(today_match.group(1)),
            minute=int(today_match.group(2)),
            second=0,
            microsecond=0,
        )

    yesterday_match = re.match(r"昨天\s*(\d{1,2}):(\d{2})", text)
    if yesterday_match:
        candidate = now - timedelta(days=1)
        return candidate.replace(
            hour=int(yesterday_match.group(1)),
            minute=int(yesterday_match.group(2)),
            second=0,
            microsecond=0,
        )

    month_day_time = re.match(r"(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})", text)
    if month_day_time:
        return datetime(
            year=now.year,
            month=int(month_day_time.group(1)),
            day=int(month_day_time.group(2)),
            hour=int(month_day_time.group(3)),
            minute=int(month_day_time.group(4)),
        )

    month_day = re.match(r"(\d{1,2})-(\d{1,2})$", text)
    if month_day:
        return datetime(
            year=now.year,
            month=int(month_day.group(1)),
            day=int(month_day.group(2)),
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )

    try:
        return date_parser.parse(text)
    except (ValueError, OverflowError):
        return None

