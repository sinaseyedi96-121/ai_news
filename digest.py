"""Recap posts built from post_history.json's posted entries.

Two digests, both linking back to the channel's own Telegram posts (not the
original news URLs) so readers stay in-channel:
  - Daily top-N: the day's best posts, fires once per local day.
  - Weekly top-N: the best posts over the trailing 7 days, fires once per week.

Both are time-triggered (checked on every run) rather than tied to fresh news,
so they still fire on runs where nothing new was fetched. Idempotency is
tracked in history["_digest_state"], which - unlike "_meta" - is never wiped
by dedupe.ensure_today's daily reset.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config
import dedupe
import publish

logger = logging.getLogger(__name__)

PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}


def _channel_link(message_id: int | None) -> str | None:
    if not config.TELEGRAM_CHANNEL_USERNAME or message_id is None:
        return None
    return f"https://t.me/{config.TELEGRAM_CHANNEL_USERNAME}/{message_id}"


def _top_entries(entries: list[dict], n: int) -> list[dict]:
    def sort_key(entry: dict):
        try:
            posted_at = datetime.fromisoformat(entry["posted_at"]).timestamp()
        except (KeyError, ValueError):
            posted_at = 0.0
        return (PRIORITY_RANK.get(entry.get("priority"), 2), -posted_at)

    return sorted(entries, key=sort_key)[:n]


def _digest_line(rank: int, entry: dict) -> str:
    title = entry.get("post_title") or entry.get("title", "")
    link = _channel_link(entry.get("message_id"))
    if link is None:
        logger.warning("No TELEGRAM_CHANNEL_USERNAME configured - digest entry has no link")
        return f"{rank}. {title}"
    return f"{rank}. {title}\n{link}"


def build_daily_digest(history: dict) -> str | None:
    local_midnight = datetime.now(ZoneInfo(config.POSTING_TIMEZONE)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    cutoff = local_midnight.astimezone(timezone.utc)
    entries = [e for e in dedupe.posted_entries_since(history, cutoff) if e.get("message_id")]
    top = _top_entries(entries, config.DAILY_DIGEST_TOP_N)
    if len(top) < config.DAILY_DIGEST_MIN_ITEMS:
        return None
    lines = ["📊 Today's top AI stories:"] + [_digest_line(i, e) for i, e in enumerate(top, start=1)]
    return "\n\n".join(lines)


def build_weekly_digest(history: dict) -> str | None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    entries = [e for e in dedupe.posted_entries_since(history, cutoff) if e.get("message_id")]
    top = _top_entries(entries, config.WEEKLY_DIGEST_TOP_N)
    if len(top) < config.WEEKLY_DIGEST_MIN_ITEMS:
        return None
    lines = ["🗓️ This week in AI — top stories:"] + [_digest_line(i, e) for i, e in enumerate(top, start=1)]
    return "\n\n".join(lines)


def maybe_post_daily_digest(history: dict) -> None:
    now_local = datetime.now(ZoneInfo(config.POSTING_TIMEZONE))
    if now_local.hour < config.DAILY_DIGEST_HOUR:
        return
    state = dedupe.get_digest_state(history)
    today_str = now_local.strftime("%Y-%m-%d")
    if state.get("daily_digest_date") == today_str:
        return

    text = build_daily_digest(history)
    if text is None:
        logger.info("Skipping daily digest - not enough posted items today")
    else:
        message_id = publish.send_text(text)
        if message_id is not None:
            logger.info("Posted daily digest (message_id=%s)", message_id)
    state["daily_digest_date"] = today_str


def maybe_post_weekly_digest(history: dict) -> None:
    now_local = datetime.now(ZoneInfo(config.POSTING_TIMEZONE))
    if now_local.weekday() != config.WEEKLY_DIGEST_WEEKDAY or now_local.hour < config.WEEKLY_DIGEST_HOUR:
        return
    state = dedupe.get_digest_state(history)
    today_str = now_local.strftime("%Y-%m-%d")
    if state.get("weekly_digest_date") == today_str:
        return

    text = build_weekly_digest(history)
    if text is None:
        logger.info("Skipping weekly digest - not enough posted items this week")
    else:
        message_id = publish.send_text(text)
        if message_id is not None:
            logger.info("Posted weekly digest (message_id=%s)", message_id)
    state["weekly_digest_date"] = today_str
