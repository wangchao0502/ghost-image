from __future__ import annotations

import logging
import random
import re
from datetime import datetime
from typing import Callable

from playwright.sync_api import Page

from .models import CrawlerConfig, ImageRecord, RawPost
from .utils import (
    build_record_id,
    canonicalize_image_url,
    is_content_image_url,
    month_bucket,
    normalize_text,
    parse_weibo_time,
    sha1_short,
    sleep_jitter,
    upgrade_image_url,
)

POST_EXTRACTION_JS = """
() => {
  const postSelectors = [
    'article',
    'div[action-type="feed_list_item"]',
    'div[class*="card-wrap"]',
    'div[class*="Feed_wrap"]'
  ];

  const imagePattern = /\\.(jpg|jpeg|png|webp|gif)(\\?|$)/i;
  const blockedTokens = [
    'vip_top_default',
    'timeline_card_small_video_default',
    'feed_icon_',
    'svip_',
    '/upload/',
    '/avatar/',
    '/icon/',
    '/badge/'
  ];
  const nodes = [];
  for (const selector of postSelectors) {
    document.querySelectorAll(selector).forEach((el) => nodes.push(el));
  }

  const seen = new Set();
  const items = [];
  for (const node of nodes) {
    if (seen.has(node)) {
      continue;
    }
    seen.add(node);

    const postLinkCandidates = Array.from(node.querySelectorAll('a[href]'));
    const postLinkNode = postLinkCandidates.find((a) => /\\/status\\/[A-Za-z0-9]{6,}/i.test(a.href))
      || postLinkCandidates.find((a) => /weibo\\.com\\/\\d+\\/[A-Za-z0-9]{8,}/i.test(a.href))
      || null;
    const postUrl = postLinkNode ? postLinkNode.href : '';
    if (!postUrl) {
      continue;
    }

    const postIdMatch = postUrl.match(/\\/status\\/([A-Za-z0-9]{6,})/i) || postUrl.match(/weibo\\.com\\/\\d+\\/([A-Za-z0-9]{8,})/i);
    const fallbackId = node.getAttribute('mid') || node.getAttribute('data-mid') || '';
    const postId = (postIdMatch && postIdMatch[1]) ? postIdMatch[1] : fallbackId;

    const contentNode =
      node.querySelector('div[node-type="feed_list_content"]') ||
      node.querySelector('div[class*="detail_wbtext"]') ||
      node.querySelector('div[class*="txt"]');

    const weiboText = contentNode ? contentNode.innerText || '' : '';

    const timeNode =
      node.querySelector('a[node-type="feed_list_item_date"]') ||
      node.querySelector('a[title][href*="/status/"]') ||
      node.querySelector('a[title][href*="weibo.com/"]') ||
      node.querySelector('span[class*="time"]') ||
      node.querySelector('a[class*="from"]');

    const publishText = timeNode ? (timeNode.innerText || '') : '';
    const publishTitle = timeNode ? (timeNode.getAttribute('title') || '') : '';

    const imageCandidates = new Set();

    node.querySelectorAll('a[href*="sinaimg.cn"]').forEach((a) => {
      const href = a.href || '';
      if (!href) {
        return;
      }
      if (imagePattern.test(href) || href.includes('sinaimg.cn')) {
        imageCandidates.add(href);
      }
    });

    node.querySelectorAll('img').forEach((img) => {
      const src = img.getAttribute('src') || img.getAttribute('data-src') || '';
      if (!src) {
        return;
      }
      const fullSrc = src.startsWith('//') ? `https:${src}` : src;
      const parentLink = img.closest('a[href]');
      const inMediaBlock =
        !!img.closest('[class*="media"]') ||
        !!img.closest('[class*="picture"]') ||
        !!img.closest('[class*="img"]') ||
        !!img.closest('[node-type*="media"]') ||
        !!img.closest('[node-type*="pic"]');
      const likelyContentImage = inMediaBlock || (parentLink && parentLink.href.includes('sinaimg.cn'));
      if (!likelyContentImage) {
        return;
      }
      if (imagePattern.test(fullSrc) || fullSrc.includes('sinaimg.cn')) {
        imageCandidates.add(fullSrc);
      }
    });

    const uniqImageUrls = Array.from(imageCandidates).filter((url) => {
      const lower = (url || '').toLowerCase();
      if (!lower.includes('sinaimg.cn')) {
        return false;
      }
      if (lower.includes('h5.sinaimg.cn') || lower.includes('d.sinaimg.cn')) {
        return false;
      }
      return !blockedTokens.some((token) => lower.includes(token));
    });
    if (!uniqImageUrls.length) {
      continue;
    }

    items.push({
      post_id: postId || '',
      post_url: postUrl,
      publish_text: publishText,
      publish_title: publishTitle,
      weibo_text: weiboText,
      image_urls: uniqImageUrls
    });
  }
  return items;
}
"""

DETAIL_EXTRACTION_JS = """
() => {
  const textNode =
    document.querySelector('div[node-type="feed_list_content_full"]') ||
    document.querySelector('div[node-type="feed_list_content"]') ||
    document.querySelector('div[class*="detail_wbtext"]') ||
    document.querySelector('div[class*="Detail_text"]') ||
    document.querySelector('article div[class*="txt"]');

  const timeNode =
    document.querySelector('a[node-type="feed_list_item_date"][title]') ||
    document.querySelector('a[class*="head-info_time"][title]') ||
    document.querySelector('div[class*="from"] a[title]') ||
    document.querySelector('time[datetime]');

  const text = textNode ? (textNode.innerText || "") : "";
  const publishText = timeNode ? (timeNode.innerText || "") : "";
  const publishTitle =
    (timeNode && (timeNode.getAttribute('title') || timeNode.getAttribute('datetime') || "")) || "";

  const ogTime = document.querySelector('meta[property="article:published_time"]');
  const ogTimeValue = ogTime ? (ogTime.getAttribute('content') || "") : "";

  return {
    weibo_text: text,
    publish_text: publishText,
    publish_title: publishTitle || ogTimeValue
  };
}
"""

STATUS_API_FETCH_JS = """
async (postId) => {
  const url = `https://weibo.com/ajax/statuses/show?id=${encodeURIComponent(postId)}&locale=zh-CN&isGetLongText=true`;
  const resp = await fetch(url, {
    credentials: 'include',
    headers: {
      'Accept': 'application/json, text/plain, */*',
      'X-Requested-With': 'XMLHttpRequest'
    }
  });
  const text = await resp.text();
  if (!resp.ok) {
    return { ok: false, status: resp.status, text };
  }
  try {
    return { ok: true, status: resp.status, data: JSON.parse(text) };
  } catch (err) {
    return { ok: false, status: resp.status, text };
  }
}
"""


def _to_raw_post(payload: dict) -> RawPost:
    return RawPost(
        post_id=(payload.get("post_id") or "").strip(),
        post_url=(payload.get("post_url") or "").strip(),
        publish_text=(payload.get("publish_text") or "").strip(),
        publish_title=(payload.get("publish_title") or "").strip(),
        weibo_text=(payload.get("weibo_text") or "").strip(),
        image_urls=[str(x).strip() for x in payload.get("image_urls") or [] if str(x).strip()],
    )


def _extract_post_id_fallback(post_url: str) -> str:
    match = re.search(r"/status/([A-Za-z0-9]{6,})", post_url) or re.search(
        r"weibo\.com/\d+/([A-Za-z0-9]{8,})", post_url
    )
    if match:
        return match.group(1)
    return sha1_short(post_url, length=10)


def _is_valid_post_id(post_id: str) -> bool:
    if not post_id:
        return False
    if post_id.isdigit() and len(post_id) >= 8:
        return True
    return bool(re.fullmatch(r"[A-Za-z0-9]{8,}", post_id))


def _fetch_status_via_api(page: Page, post_id: str) -> dict | None:
    if not _is_valid_post_id(post_id):
        return None
    try:
        payload = page.evaluate(STATUS_API_FETCH_JS, post_id) or {}
    except Exception:
        return None
    if not payload.get("ok"):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    return data


def _extract_api_image_urls(status_data: dict, image_quality: str) -> list[str]:
    pic_infos = status_data.get("pic_infos") or {}
    if not isinstance(pic_infos, dict):
        return []
    urls: list[str] = []
    for item in pic_infos.values():
        if not isinstance(item, dict):
            continue
        original = (item.get("original") or {}).get("url")
        largest = (item.get("largest") or {}).get("url")
        large = (item.get("large") or {}).get("url")
        chosen = original or largest or large
        if not chosen:
            continue
        normalized = canonicalize_image_url(upgrade_image_url(chosen, image_quality=image_quality))
        if is_content_image_url(normalized):
            urls.append(normalized)
    return list(dict.fromkeys(urls))


def _scroll_like_human(page: Page, config: CrawlerConfig) -> None:
    viewport_height = page.evaluate("() => window.innerHeight || 900")
    step = int(viewport_height * random.uniform(0.55, 1.35))
    page.mouse.wheel(0, step)
    sleep_jitter(config.min_scroll_delay, config.max_scroll_delay)


def collect_records(page: Page, config: CrawlerConfig, existing_record_ids: set[str]) -> list[ImageRecord]:
    records: list[ImageRecord] = []
    discovered_ids = set(existing_record_ids)
    stagnant_rounds = 0

    for _ in range(config.max_rounds):
        payload = page.evaluate(POST_EXTRACTION_JS)
        round_new = 0
        now = datetime.now()
        for item in payload:
            post = _to_raw_post(item)
            if not post.post_url:
                continue
            post_id = post.post_id or _extract_post_id_fallback(post.post_url)
            publish_dt = parse_weibo_time(post.publish_text, post.publish_title)
            publish_month = month_bucket(publish_dt)
            normalized_text = normalize_text(post.weibo_text)

            for image_url in post.image_urls:
                upgraded_url = upgrade_image_url(image_url, image_quality=config.image_quality)
                if not is_content_image_url(upgraded_url):
                    continue
                canonical_url = canonicalize_image_url(upgraded_url)
                record_id = build_record_id(config.blogger_id, post_id, canonical_url)
                if record_id in discovered_ids:
                    continue
                discovered_ids.add(record_id)
                round_new += 1
                records.append(
                    ImageRecord(
                        record_id=record_id,
                        blogger_id=config.blogger_id,
                        blogger_name=config.blogger_name,
                        post_id=post_id,
                        post_url=post.post_url,
                        image_url=canonical_url,
                        published_at=publish_dt,
                        published_month=publish_month,
                        weibo_text_raw=post.weibo_text,
                        weibo_text_normalized=normalized_text,
                        local_path="",
                        crawl_time=now,
                        status="discovered",
                    )
                )
                if config.max_items and len(records) >= config.max_items:
                    return records

        if round_new == 0:
            stagnant_rounds += 1
        else:
            stagnant_rounds = 0

        if stagnant_rounds >= config.stagnation_rounds:
            break

        _scroll_like_human(page, config)
        # API hydration runs per-post; keep a tiny jitter to avoid long full-crawl latency.
        sleep_jitter(0.05, 0.2)

    return records


def hydrate_records_with_status_api(
    page: Page, records: list[ImageRecord], config: CrawlerConfig, existing_record_ids: set[str]
) -> list[ImageRecord]:
    """
    Use statuses/show API as source of truth:
    - complete high-res content images per post
    - weibo text
    - publish time
    """
    output: list[ImageRecord] = []
    seen_ids = set(existing_record_ids)

    by_post: dict[str, list[ImageRecord]] = {}
    for record in records:
        by_post.setdefault(record.post_id, []).append(record)

    for post_id, post_records in by_post.items():
        status_data = _fetch_status_via_api(page, post_id)
        if not status_data:
            # Keep filtered fallback records when API is unavailable.
            for record in post_records:
                if record.record_id in seen_ids:
                    continue
                seen_ids.add(record.record_id)
                output.append(record)
            continue

        now = datetime.now()
        publish_dt = parse_weibo_time(status_data.get("created_at") or "", "")
        publish_month = month_bucket(publish_dt)
        text_raw = (
            (status_data.get("longText") or {}).get("longTextContent")
            or status_data.get("text_raw")
            or post_records[0].weibo_text_raw
        )
        text_norm = normalize_text(text_raw)
        post_url = post_records[0].post_url
        mblogid = status_data.get("mblogid")
        user_id = ((status_data.get("user") or {}).get("idstr") or "").strip()
        if mblogid and user_id:
            post_url = f"https://weibo.com/{user_id}/{mblogid}"

        api_image_urls = _extract_api_image_urls(status_data, image_quality=config.image_quality)
        if not api_image_urls:
            # fallback: preserve already filtered DOM URLs
            api_image_urls = list(dict.fromkeys(r.image_url for r in post_records if is_content_image_url(r.image_url)))

        for image_url in api_image_urls:
            canonical = canonicalize_image_url(image_url)
            record_id = build_record_id(config.blogger_id, post_id, canonical)
            if record_id in seen_ids:
                continue
            seen_ids.add(record_id)
            output.append(
                ImageRecord(
                    record_id=record_id,
                    blogger_id=config.blogger_id,
                    blogger_name=((status_data.get("user") or {}).get("screen_name") or config.blogger_name),
                    post_id=post_id,
                    post_url=post_url,
                    image_url=canonical,
                    published_at=publish_dt,
                    published_month=publish_month,
                    weibo_text_raw=text_raw or "",
                    weibo_text_normalized=text_norm,
                    local_path="",
                    crawl_time=now,
                    status="discovered",
                )
            )

        sleep_jitter(config.min_round_cooldown, config.max_round_cooldown)

    return output


def stream_records_api_first(
    page: Page,
    config: CrawlerConfig,
    existing_record_ids: set[str],
    on_records: Callable[[list[ImageRecord]], bool],
    logger: logging.Logger | None = None,
) -> int:
    """
    Stream records while scrolling:
    - discover posts from current viewport
    - hydrate each post by statuses/show API immediately
    - emit records batch to callback for immediate metadata+download scheduling
    """
    seen_record_ids = set(existing_record_ids)
    seen_post_ids: set[str] = set()
    stagnant_rounds = 0
    emitted_total = 0

    for _ in range(config.max_rounds):
        round_index = _ + 1
        payload = page.evaluate(POST_EXTRACTION_JS)
        candidates: list[RawPost] = []
        for item in payload:
            post = _to_raw_post(item)
            if not post.post_url:
                continue
            post_id = post.post_id or _extract_post_id_fallback(post.post_url)
            if not _is_valid_post_id(post_id):
                continue
            if post_id in seen_post_ids:
                continue
            seen_post_ids.add(post_id)
            post.post_id = post_id
            candidates.append(post)
        if logger:
            logger.info(
                "round=%s candidates=%s seen_posts=%s emitted_total=%s",
                round_index,
                len(candidates),
                len(seen_post_ids),
                emitted_total,
            )

        round_records: list[ImageRecord] = []
        for post in candidates:
            status_data = _fetch_status_via_api(page, post.post_id)
            now = datetime.now()

            if status_data:
                publish_dt = parse_weibo_time(status_data.get("created_at") or "", "")
                publish_month = month_bucket(publish_dt)
                text_raw = (status_data.get("longText") or {}).get("longTextContent") or status_data.get("text_raw") or ""
                text_norm = normalize_text(text_raw)
                post_url = post.post_url
                mblogid = status_data.get("mblogid")
                user_id = ((status_data.get("user") or {}).get("idstr") or "").strip()
                if mblogid and user_id:
                    post_url = f"https://weibo.com/{user_id}/{mblogid}"
                image_urls = _extract_api_image_urls(status_data, image_quality=config.image_quality)
            else:
                publish_dt = parse_weibo_time(post.publish_text, post.publish_title)
                publish_month = month_bucket(publish_dt)
                text_raw = post.weibo_text
                text_norm = normalize_text(text_raw)
                post_url = post.post_url
                image_urls = []
                for u in post.image_urls:
                    upgraded = upgrade_image_url(u, image_quality=config.image_quality)
                    if is_content_image_url(upgraded):
                        image_urls.append(canonicalize_image_url(upgraded))

            for image_url in image_urls:
                record_id = build_record_id(config.blogger_id, post.post_id, image_url)
                if record_id in seen_record_ids:
                    continue
                seen_record_ids.add(record_id)
                round_records.append(
                    ImageRecord(
                        record_id=record_id,
                        blogger_id=config.blogger_id,
                        blogger_name=((status_data.get("user") or {}).get("screen_name") or config.blogger_name)
                        if status_data
                        else config.blogger_name,
                        post_id=post.post_id,
                        post_url=post_url,
                        image_url=image_url,
                        published_at=publish_dt,
                        published_month=publish_month,
                        weibo_text_raw=text_raw or "",
                        weibo_text_normalized=text_norm,
                        local_path="",
                        crawl_time=now,
                        status="discovered",
                    )
                )
                emitted_total += 1
                if config.max_items and emitted_total >= config.max_items:
                    break

            sleep_jitter(0.05, 0.2)
            if config.max_items and emitted_total >= config.max_items:
                break

        if round_records:
            stagnant_rounds = 0
            if logger:
                logger.info("round=%s new_records=%s", round_index, len(round_records))
            should_continue = on_records(round_records)
            if not should_continue:
                if logger:
                    logger.info("streaming halted by callback at round=%s", round_index)
                break
        else:
            stagnant_rounds += 1
            if logger:
                logger.info("round=%s no_new_records stagnant=%s", round_index, stagnant_rounds)
            if stagnant_rounds >= config.stagnation_rounds:
                if logger:
                    logger.info("stop by stagnation threshold at round=%s", round_index)
                break

        _scroll_like_human(page, config)
        sleep_jitter(0.05, 0.2)
        if config.max_items and emitted_total >= config.max_items:
            if logger:
                logger.info("stop by max_items=%s at round=%s", config.max_items, round_index)
            break

    if logger:
        logger.info("stream_records done emitted_total=%s", emitted_total)
    return emitted_total


def enrich_records_with_post_details(page: Page, records: list[ImageRecord], config: CrawlerConfig) -> None:
    """
    Fill missing weibo text / publish time by visiting unique post detail pages.
    Keeps behavior read-only and low-frequency.
    """
    by_post: dict[str, list[ImageRecord]] = {}
    for record in records:
        by_post.setdefault(record.post_url, []).append(record)

    for post_url, post_records in by_post.items():
        needs_enrich = any((not r.weibo_text_raw.strip()) or (r.published_at is None) for r in post_records)
        if not needs_enrich:
            continue

        try:
            page.goto(post_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2200)
            payload = page.evaluate(DETAIL_EXTRACTION_JS) or {}
        except Exception:
            continue

        text_raw = (payload.get("weibo_text") or "").strip()
        text_norm = normalize_text(text_raw)
        publish_dt = parse_weibo_time(payload.get("publish_text") or "", payload.get("publish_title") or "")
        publish_month = month_bucket(publish_dt)

        for record in post_records:
            if text_raw and not record.weibo_text_raw.strip():
                record.weibo_text_raw = text_raw
                record.weibo_text_normalized = text_norm
            if publish_dt and record.published_at is None:
                record.published_at = publish_dt
                record.published_month = publish_month

        sleep_jitter(config.min_round_cooldown, config.max_round_cooldown)

