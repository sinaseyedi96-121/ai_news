"""Recap posts built from post_history.json's posted entries.

Two digests, both linking back to a channel's own Telegram posts (not the
original news URLs) so readers stay in-channel:
  - Daily top-N: the day's best posts, fires once per local day.
  - Weekly top-N: the best posts over the trailing 7 days, fires once per week.

Each digest is emitted once per configured language (English + optional Farsi
mirror). A per-language spec (_LANGS) picks the channel handle, the title and
message-id fields to read from each history entry, the header wording, and the
idempotency key - so the Farsi recap links to Farsi posts and fires
independently of the English one.

Both are time-triggered (checked on every run) rather than tied to fresh news,
so they still fire on runs where nothing new was fetched. Idempotency is
tracked in history["_digest_state"], which - unlike "_meta" - is never wiped
by dedupe.ensure_today's daily reset.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config
import dedupe
import publish

logger = logging.getLogger(__name__)

PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}


@dataclass(frozen=True)
class _Lang:
    """Everything that differs between the English and Farsi digests."""
    lang: str                    # "en" / "fa" - also selects publish channel
    message_id_key: str          # history-entry key holding this channel's message id
    title_keys: tuple[str, ...]  # entry keys tried in order for the display title
    daily_header: str
    weekly_header: str
    daily_state_key: str         # _digest_state key for once-per-day idempotency
    weekly_state_key: str


def _langs() -> list[_Lang]:
    """Read config.*_USERNAME live via publish, so env/test overrides apply."""
    return [
        _Lang(
            "en", "message_id", ("post_title", "title"),
            "📊 Today's top AI stories:",
            "🗓️ This week in AI — top stories:",
            "daily_digest_date", "weekly_digest_date",
        ),
        _Lang(
            "fa", "message_id_fa", ("post_title_fa", "post_title", "title"),
            "📊 برترین اخبار هوش مصنوعی امروز:",
            "🗓️ هفته‌ای که گذشت — مهم‌ترین اخبار هوش مصنوعی:",
            "daily_digest_date_fa", "weekly_digest_date_fa",
        ),
    ]


def _channel_username(lang: str) -> str:
    return (config.TELEGRAM_FARSI_CHANNEL_USERNAME if lang == "fa"
            else config.TELEGRAM_CHANNEL_USERNAME)


def _channel_link(lang: str, message_id: int | None) -> str | None:
    username = _channel_username(lang)
    if not username or message_id is None:
        return None
    return f"https://t.me/{username}/{message_id}"


def _display_title(entry: dict, spec: _Lang) -> str:
    for key in spec.title_keys:
        value = entry.get(key)
        if value:
            return value
    return ""


def _top_entries(entries: list[dict], n: int) -> list[dict]:
    def sort_key(entry: dict):
        try:
            posted_at = datetime.fromisoformat(entry["posted_at"]).timestamp()
        except (KeyError, ValueError):
            posted_at = 0.0
        return (PRIORITY_RANK.get(entry.get("priority"), 2), -posted_at)

    return sorted(entries, key=sort_key)[:n]


def _digest_line(rank: int, entry: dict, spec: _Lang) -> str:
    title = publish.rtl_anchor(_display_title(entry, spec), spec.lang)
    link = _channel_link(spec.lang, entry.get(spec.message_id_key))
    if link is None:
        logger.warning("No %s channel username - digest entry has no link", spec.lang)
        return f"{rank}. {title}"
    return f"{rank}. {title}\n{link}"


def _build_digest(entries: list[dict], header: str, top_n: int, min_items: int,
                  spec: _Lang) -> str | None:
    # Only entries actually posted to *this* channel (have its message id) can
    # be linked, so a Farsi digest never references a post that isn't in the
    # Farsi channel.
    entries = [e for e in entries if e.get(spec.message_id_key)]
    top = _top_entries(entries, top_n)
    if len(top) < min_items:
        return None
    lines = [header] + [_digest_line(i, e, spec) for i, e in enumerate(top, start=1)]
    return "\n\n".join(lines)


def build_daily_digest(history: dict, spec: _Lang) -> str | None:
    local_midnight = datetime.now(ZoneInfo(config.POSTING_TIMEZONE)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    cutoff = local_midnight.astimezone(timezone.utc)
    entries = dedupe.posted_entries_since(history, cutoff)
    return _build_digest(entries, spec.daily_header, config.DAILY_DIGEST_TOP_N,
                         config.DAILY_DIGEST_MIN_ITEMS, spec)


def build_weekly_digest(history: dict, spec: _Lang) -> str | None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    entries = dedupe.posted_entries_since(history, cutoff)
    return _build_digest(entries, spec.weekly_header, config.WEEKLY_DIGEST_TOP_N,
                         config.WEEKLY_DIGEST_MIN_ITEMS, spec)


def maybe_post_daily_digest(history: dict) -> None:
    now_local = datetime.now(ZoneInfo(config.POSTING_TIMEZONE))
    if now_local.hour < config.DAILY_DIGEST_HOUR:
        return
    state = dedupe.get_digest_state(history)
    today_str = now_local.strftime("%Y-%m-%d")
    for spec in _langs():
        if not publish.channel_configured(spec.lang):
            continue
        if state.get(spec.daily_state_key) == today_str:
            continue
        text = build_daily_digest(history, spec)
        if text is None:
            logger.info("Skipping %s daily digest - not enough posted items today", spec.lang)
        else:
            message_id = publish.send_text(text, spec.lang)
            if message_id is not None:
                logger.info("Posted %s daily digest (message_id=%s)", spec.lang, message_id)
        state[spec.daily_state_key] = today_str


def maybe_post_weekly_digest(history: dict) -> None:
    now_local = datetime.now(ZoneInfo(config.POSTING_TIMEZONE))
    if now_local.weekday() != config.WEEKLY_DIGEST_WEEKDAY or now_local.hour < config.WEEKLY_DIGEST_HOUR:
        return
    state = dedupe.get_digest_state(history)
    today_str = now_local.strftime("%Y-%m-%d")
    for spec in _langs():
        if not publish.channel_configured(spec.lang):
            continue
        if state.get(spec.weekly_state_key) == today_str:
            continue
        text = build_weekly_digest(history, spec)
        if text is None:
            logger.info("Skipping %s weekly digest - not enough posted items this week", spec.lang)
        else:
            message_id = publish.send_text(text, spec.lang)
            if message_id is not None:
                logger.info("Posted %s weekly digest (message_id=%s)", spec.lang, message_id)
        state[spec.weekly_state_key] = today_str
