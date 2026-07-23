"""Dedup layer.

Two layers, both needed because this runs on ephemeral GitHub Actions runners:
  A. Canonical URL hash - cheap, exact match on the same link seen again.
  B. Title similarity - catches the same underlying story republished under a
     different URL by a different outlet (rapidfuzz token_sort_ratio over a
     rolling window, since comparing against months of history isn't useful
     and gets slower over time).

State lives in post_history.json, keyed by url hash, plus a "_meta" entry that
tracks the daily posting cap (see config.MAX_POSTS_PER_DAY).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

from rapidfuzz import fuzz

import config

logger = logging.getLogger(__name__)

_TRACKING_PARAM_PREFIXES = ("utm_",)
_TRACKING_PARAMS = {"ref", "fbclid", "gclid", "igshid", "mc_cid", "mc_eid"}
_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    kept_params = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS and not k.lower().startswith(_TRACKING_PARAM_PREFIXES)
    ]
    path = parsed.path.rstrip("/") or "/"
    netloc = parsed.netloc.lower()
    return urlunparse(("https", netloc, path, "", urlencode(sorted(kept_params)), ""))


def url_hash(url: str) -> str:
    return sha256(canonicalize_url(url).encode("utf-8")).hexdigest()


def normalize_title(title: str) -> str:
    return _WS_RE.sub(" ", _PUNCT_RE.sub("", title.lower())).strip()


_RESERVED_KEYS = ("_meta", "_digest_state")


def default_meta() -> dict:
    return {"date": None, "posts_today": 0, "posts_today_by_category": {}}


def default_digest_state() -> dict:
    """Tracks the last date each digest was posted, so a slow-fire hourly cron
    doesn't repost it every run within the same day/week. Unlike "_meta", this
    is never reset by ensure_today - it needs to persist across day changes.
    The "_fa" keys track the Farsi mirror's digests independently, so each
    channel can post its own recap without blocking the other."""
    return {
        "daily_digest_date": None, "weekly_digest_date": None,
        "daily_digest_date_fa": None, "weekly_digest_date_fa": None,
    }


def load_history(path=config.POST_HISTORY_PATH) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        history = {}
    history.setdefault("_meta", default_meta())
    history.setdefault("_digest_state", default_digest_state())
    ensure_today(history)
    return history


def ensure_today(history: dict) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if history["_meta"].get("date") != today:
        history["_meta"] = {"date": today, "posts_today": 0, "posts_today_by_category": {}}


def save_history(history: dict, path=config.POST_HISTORY_PATH) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.HISTORY_RETENTION_DAYS)
    pruned = {"_meta": history["_meta"], "_digest_state": history["_digest_state"]}
    for key, entry in history.items():
        if key in _RESERVED_KEYS:
            continue
        try:
            seen_at = datetime.fromisoformat(entry["seen_at"])
        except (KeyError, ValueError):
            continue
        if seen_at >= cutoff:
            pruned[key] = entry
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pruned, f, indent=2, sort_keys=True)
        f.write("\n")


def _title_window_entries(history: dict):
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.TITLE_DEDUPE_WINDOW_DAYS)
    for key, entry in history.items():
        if key in _RESERVED_KEYS:
            continue
        try:
            seen_at = datetime.fromisoformat(entry["seen_at"])
        except (KeyError, ValueError):
            continue
        if seen_at >= cutoff:
            yield entry


def is_duplicate(item: dict, history: dict) -> bool:
    if url_hash(item["url"]) in history:
        return True
    title_norm = normalize_title(item["title"])
    for entry in _title_window_entries(history):
        if fuzz.token_sort_ratio(title_norm, entry.get("title_norm", "")) >= config.TITLE_DEDUPE_THRESHOLD:
            return True
    return False


def filter_new_items(items: list[dict], history: dict) -> list[dict]:
    """Drop items already in history, and duplicates within this same batch."""
    new_items = []
    seen_hashes = set()
    for item in items:
        h = url_hash(item["url"])
        if h in seen_hashes:
            continue
        if is_duplicate(item, history):
            continue
        seen_hashes.add(h)
        new_items.append(item)
    logger.info("Dedup: %d/%d items are new", len(new_items), len(items))
    return new_items


def add_history_entry(history: dict, item: dict, posted: bool,
                      message_id: int | None = None, message_id_fa: int | None = None) -> None:
    seen_at = datetime.now(timezone.utc).isoformat()
    entry = {
        "title": item["title"],
        "title_norm": normalize_title(item["title"]),
        "source": item["source"],
        "category": item["category"],
        "seen_at": seen_at,
        "posted": posted,
    }
    if posted:
        # Extra fields only needed for digest posts (main.select_for_publishing
        # already applied priority; the message ids let each channel's digest
        # link back to its own post instead of the original news URL - "_fa" is
        # the Farsi mirror's message id, absent/None when it wasn't posted there).
        entry["posted_at"] = seen_at
        entry["message_id"] = message_id
        entry["message_id_fa"] = message_id_fa
        entry["post_title"] = item.get("post_title")
        entry["post_title_fa"] = item.get("post_title_fa")
        entry["priority"] = item.get("priority")
    history[url_hash(item["url"])] = entry


def posted_entries_since(history: dict, cutoff: datetime) -> list[dict]:
    """Posted entries with posted_at >= cutoff, for building digest posts."""
    entries = []
    for key, entry in history.items():
        if key in _RESERVED_KEYS or not entry.get("posted"):
            continue
        try:
            posted_at = datetime.fromisoformat(entry["posted_at"])
        except (KeyError, ValueError):
            continue
        if posted_at >= cutoff:
            entries.append(entry)
    return entries


def get_meta(history: dict) -> dict:
    return history["_meta"]


def get_digest_state(history: dict) -> dict:
    return history["_digest_state"]
