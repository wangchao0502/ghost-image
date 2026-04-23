from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_random_exponential

from .models import CrawlerConfig, ImageRecord
from .utils import (
    build_record_id,
    canonicalize_image_url,
    ensure_dir,
    extension_from_url,
    extract_blogger_id_from_url,
    sanitize_filename,
    sha1_short,
    sleep_jitter,
)


def _normalize_metadata_blogger_fields(
    payload: dict, default_blogger_id: str, default_blogger_name: str
) -> tuple[dict, bool]:
    changed = False
    blogger_id = str(payload.get("blogger_id") or "").strip()
    if not blogger_id:
        blogger_id = extract_blogger_id_from_url(str(payload.get("post_url") or "")) or default_blogger_id
        if blogger_id:
            payload["blogger_id"] = blogger_id
            changed = True
    blogger_name = str(payload.get("blogger_name") or "").strip()
    if not blogger_name and default_blogger_name:
        payload["blogger_name"] = default_blogger_name
        changed = True
    elif "blogger_name" not in payload:
        payload["blogger_name"] = blogger_name
        changed = True
    return payload, changed


def migrate_metadata_schema(metadata_path: Path, default_blogger_id: str, default_blogger_name: str) -> int:
    if not metadata_path.exists():
        return 0
    lines = metadata_path.read_text(encoding="utf-8").splitlines()
    if not lines:
        return 0
    migrated = 0
    output_lines: list[str] = []
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            output_lines.append(text)
            continue
        payload, changed = _normalize_metadata_blogger_fields(payload, default_blogger_id, default_blogger_name)
        if changed:
            migrated += 1
        output_lines.append(json.dumps(payload, ensure_ascii=False))
    if migrated:
        metadata_path.write_text("\n".join(output_lines) + "\n", encoding="utf-8")
    return migrated


def load_existing_record_ids(metadata_path: Path, blogger_id: str) -> set[str]:
    finalized_statuses = {"downloaded", "skipped_existing", "metadata_only"}
    record_ids: set[str] = set()
    if not metadata_path.exists():
        return record_ids
    with metadata_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = payload.get("record_id")
            status = payload.get("status")
            if isinstance(rid, str) and rid and status in finalized_statuses:
                record_ids.add(rid)
                post_id = str(payload.get("post_id") or "").strip()
                image_url = canonicalize_image_url(str(payload.get("image_url") or "").strip())
                if post_id and image_url:
                    existing_blogger_id = (
                        str(payload.get("blogger_id") or "").strip()
                        or extract_blogger_id_from_url(str(payload.get("post_url") or ""))
                        or blogger_id
                    )
                    if existing_blogger_id:
                        record_ids.add(build_record_id(existing_blogger_id, post_id, image_url))
                    # Backward-compatible dedupe for old metadata before blogger_id was introduced.
                    record_ids.add(sha1_short(f"{post_id}|{image_url}", length=16))
    return record_ids


def _serialize_record(record: ImageRecord) -> dict:
    payload = asdict(record)
    payload["published_at"] = record.published_at.isoformat() if record.published_at else None
    payload["crawl_time"] = record.crawl_time.isoformat()
    return payload


def append_metadata(records: list[ImageRecord], metadata_path: Path) -> None:
    ensure_dir(metadata_path.parent)
    with metadata_path.open("a", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(_serialize_record(record), ensure_ascii=False) + "\n")


def build_target_path(images_dir: Path, record: ImageRecord) -> Path:
    ensure_dir(images_dir)
    month_dir = images_dir / record.published_month
    ensure_dir(month_dir)

    timestamp = (
        record.published_at.strftime("%Y%m%d_%H%M%S")
        if record.published_at
        else datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    ext = extension_from_url(record.image_url)
    suffix = sha1_short(record.image_url, length=10)
    filename = sanitize_filename(f"{timestamp}_{suffix}{ext}")
    return month_dir / filename


@retry(
    retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
    stop=stop_after_attempt(3),
    wait=wait_random_exponential(multiplier=0.8, max=8),
    reraise=True,
)
def _download_once(client: httpx.Client, url: str) -> bytes:
    response = client.get(url)
    response.raise_for_status()
    return response.content


def download_record(record: ImageRecord, config: CrawlerConfig) -> ImageRecord:
    """Download one image record (thread-safe, per-call client)."""
    if config.dry_run:
        record.status = "metadata_only"
        return record

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Referer": "https://weibo.com/",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }

    target = build_target_path(config.images_dir, record)
    record.local_path = target.as_posix()
    if target.exists():
        record.status = "skipped_existing"
        return record

    try:
        with httpx.Client(timeout=config.request_timeout, headers=headers, follow_redirects=True) as client:
            content = _download_once(client, record.image_url)
        target.write_bytes(content)
        record.status = "downloaded"
    except Exception as exc:  # noqa: BLE001
        record.status = "failed"
        record.error = str(exc)
    sleep_jitter(0.15, 0.6)
    return record


def download_records(records: list[ImageRecord], config: CrawlerConfig) -> list[ImageRecord]:
    if config.dry_run:
        for rec in records:
            rec.status = "metadata_only"
        return records

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Referer": "https://weibo.com/",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }

    with httpx.Client(timeout=config.request_timeout, headers=headers, follow_redirects=True) as client:
        for rec in records:
            target = build_target_path(config.images_dir, rec)
            rec.local_path = target.as_posix()
            if target.exists():
                rec.status = "skipped_existing"
                continue
            try:
                content = _download_once(client, rec.image_url)
                target.write_bytes(content)
                rec.status = "downloaded"
            except Exception as exc:  # noqa: BLE001 - keep crawl running for per-item errors
                rec.status = "failed"
                rec.error = str(exc)
            sleep_jitter(0.2, 1.0)
    return records

