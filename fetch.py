"""Fetch layer: pull raw items from the RSS feeds in sources.json, plus a NewsAPI
keyword search for AI policy/politics (no clean dedicated RSS exists for that).

A single broken feed must never kill the run - every feed fetch is wrapped in its
own try/except with logging.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from time import mktime

import feedparser
import requests

import config

logger = logging.getLogger(__name__)

NEWSAPI_CATEGORY = "policy"


def load_sources() -> dict:
    with open(config.SOURCES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _entry_published_iso(entry) -> str:
    for field in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, field, None)
        if parsed:
            return datetime.fromtimestamp(mktime(parsed), tz=timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


def _entry_summary(entry) -> str:
    return getattr(entry, "summary", "") or getattr(entry, "description", "") or ""


def normalize_rss_entry(entry, source_name: str, category: str) -> dict | None:
    url = getattr(entry, "link", None)
    title = getattr(entry, "title", None)
    if not url or not title:
        return None
    return {
        "title": title.strip(),
        "url": url.strip(),
        "summary": _entry_summary(entry).strip(),
        "published": _entry_published_iso(entry),
        "source": source_name,
        "category": category,
    }


def fetch_rss_feed(name: str, url: str, category: str) -> list[dict]:
    """Fetch and normalize a single RSS feed. Never raises - logs and returns [] on failure."""
    items: list[dict] = []
    try:
        parsed = feedparser.parse(url)
        if parsed.bozo and not parsed.entries:
            raise parsed.bozo_exception or RuntimeError("feedparser reported bozo with no entries")
        for entry in parsed.entries:
            item = normalize_rss_entry(entry, name, category)
            if item:
                items.append(item)
    except Exception:
        logger.warning("Failed to fetch feed '%s' (%s)", name, url, exc_info=True)
    return items


def fetch_all_rss(sources: dict) -> list[dict]:
    items: list[dict] = []
    for category, feeds in sources.items():
        for feed in feeds:
            fetched = fetch_rss_feed(feed["name"], feed["url"], category)
            logger.info("Fetched %d items from %s", len(fetched), feed["name"])
            items.extend(fetched)
    return items


def fetch_newsapi_policy() -> list[dict]:
    """AI policy/regulation news via NewsAPI keyword search (free tier)."""
    if not config.NEWSAPI_KEY:
        logger.warning("NEWSAPI_KEY not set - skipping AI policy/politics fetch")
        return []

    since = datetime.now(timezone.utc) - timedelta(hours=config.NEWSAPI_LOOKBACK_HOURS)
    params = {
        "q": config.NEWSAPI_QUERY,
        "domains": config.NEWSAPI_DOMAINS,
        "from": since.strftime("%Y-%m-%dT%H:%M:%S"),
        "sortBy": "publishedAt",
        "language": "en",
        "apiKey": config.NEWSAPI_KEY,
    }

    try:
        resp = requests.get(config.NEWSAPI_URL, params=params, timeout=config.REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.warning("NewsAPI fetch failed", exc_info=True)
        return []

    items: list[dict] = []
    for article in data.get("articles", []):
        url = article.get("url")
        title = article.get("title")
        if not url or not title:
            continue
        items.append({
            "title": title.strip(),
            "url": url.strip(),
            "summary": (article.get("description") or "").strip(),
            "published": article.get("publishedAt") or datetime.now(timezone.utc).isoformat(),
            "source": (article.get("source") or {}).get("name", "NewsAPI"),
            "category": NEWSAPI_CATEGORY,
        })
    logger.info("Fetched %d items from NewsAPI (policy)", len(items))
    return items


def fetch_all() -> list[dict]:
    sources = load_sources()
    items = fetch_all_rss(sources)
    items.extend(fetch_newsapi_policy())
    return items


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Test a single feed locally before adding it to sources.json")
    parser.add_argument("--test-feed", metavar="URL", help="RSS feed URL to fetch and print")
    args = parser.parse_args()

    if args.test_feed:
        results = fetch_rss_feed("test-feed", args.test_feed, "test")
        print(f"Fetched {len(results)} items:\n")
        for r in results[:10]:
            print(f"- {r['title']}\n  {r['url']}\n")
    else:
        parser.print_help()
