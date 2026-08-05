"""Publish layer: send approved items to the Telegram channel(s) via the Bot API.

Two destinations are supported: the primary English channel and an optional
Farsi mirror (config.TELEGRAM_FARSI_*). Each classified item carries both an
English (post_title/post_body) and a Farsi (post_title_fa/post_body_fa)
rendering; we send the matching language to each channel and report both
results so the caller can record a per-channel message id (used by digest.py to
link each digest entry back to that channel's own post).

If the Farsi channel isn't configured, every Farsi send is reported as skipped
and the pipeline behaves exactly like the English-only version.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from telegram import Bot
from telegram.error import TelegramError

import config

logger = logging.getLogger(__name__)

# Rough "starts with an emoji-ish character" check, used only as a safety net
# in case DeepSeek forgets the leading emoji it was instructed to include.
# Covers arrows/symbols/dingbats (U+2190-U+2BFF), variation selector
# (U+FE0F), and the main emoji planes (U+1F000-U+1FFFF).
_EMOJI_START_RE = re.compile(
    "^[←-⯿️\U0001f000-\U0001ffff]"
)

# Right-to-left mark (zero-width, invisible): forced as the first character of
# a Farsi line so the Unicode bidi algorithm anchors the whole line as RTL.
# Without it, a line that starts with emoji + a Latin brand name (e.g. "🚀
# GPT-5 ...", kept untranslated per the prompt) has its base direction
# auto-detected from that first strong-direction character - Latin, since
# emoji are direction-neutral - which flips the whole line to LTR and makes
# the leading emoji visually jump to the end for an RTL reader.
_RLM = "\u200f"


def rtl_anchor(text: str, lang: str) -> str:
    """Prefix every line of `text` with the RLM if lang is Farsi, so each
    paragraph gets its own RTL anchor (bidi treats "\n" as a paragraph
    separator, so a body with multiple paragraphs needs one per line, not
    just one at the very start)."""
    if lang != "fa":
        return text
    return "\n".join(f"{_RLM}{line}" if line else line for line in text.split("\n"))


# Sentence-boundary heuristic used only to find where the visible preview ends:
# punctuation immediately followed by whitespace. Covers Farsi's "؟" too.
# Misses abbreviations like "U.S." (no space right after the period there
# would just mean the split lands one word later), which is an acceptable
# tradeoff for a lightweight preview cut.
_SENTENCE_END_RE = re.compile(r"(?<=[.!?؟])\s+")


def _split_preview(body: str) -> tuple[str, str]:
    """Split a post_body into (preview, hidden): preview is the first
    sentence of the first paragraph; hidden is everything else (the rest of
    that paragraph plus any further paragraphs), destined for an expandable
    blockquote. Returns hidden="" when there's nothing left to hide."""
    body = (body or "").strip()
    if not body:
        return "", ""
    first_para, *rest_paragraphs = body.split("\n\n")
    sentences = _SENTENCE_END_RE.split(first_para.strip())
    preview = sentences[0]
    remainder = " ".join(sentences[1:]).strip()
    hidden_parts = ([remainder] if remainder else []) + rest_paragraphs
    hidden = "\n\n".join(p for p in hidden_parts if p)
    return preview, hidden


def _escape_html(text: str) -> str:
    """Escape the characters Telegram's HTML parse mode treats as markup."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@dataclass(frozen=True)
class Channel:
    """A Telegram destination: which bot token, which chat, which language."""
    lang: str        # "en" or "fa" - selects the post rendering and log label
    token: str
    chat_id: str

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)


def _channel(lang: str) -> Channel:
    """Build the Channel for a language, reading config live (so env/test
    overrides applied after import are picked up)."""
    if lang == "fa":
        return Channel("fa", config.TELEGRAM_FARSI_BOT_TOKEN, config.TELEGRAM_FARSI_CHANNEL_ID)
    return Channel("en", config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHANNEL_ID)


# English first (primary); Farsi second (mirror). Order matters for logging and
# for which failure drives the "posted" flag in main.py (English = primary).
LANGS = ("en", "fa")


def channel_configured(lang: str) -> bool:
    return _channel(lang).configured


def _with_leading_emoji(text: str, category: str, lang: str = "en") -> str:
    text = (text or "").strip()
    if not (text and _EMOJI_START_RE.match(text)):
        emoji = config.CATEGORY_EMOJIS.get(category, config.DEFAULT_EMOJI)
        text = f"{emoji} {text}".strip()
    return rtl_anchor(text, lang)


def format_post(item: dict, lang: str = "en") -> str:
    """Render an item for a channel as Telegram HTML. Farsi falls back to the
    English fields if the model didn't return a translation for some reason.

    Only the title and the first sentence of the body are shown up front;
    the rest (remainder of the first paragraph + any further paragraphs) is
    wrapped in an expandable blockquote so the channel feed stays scannable
    while the full text is one tap away."""
    if lang == "fa":
        title_src = item.get("post_title_fa") or item.get("post_title")
        body_src = item.get("post_body_fa") or item.get("post_body")
    else:
        title_src = item["post_title"]
        body_src = item["post_body"]

    title = _with_leading_emoji(title_src, item["category"], lang)
    preview_raw, hidden_raw = _split_preview(body_src)
    preview = _with_leading_emoji(preview_raw, item["category"], lang)
    hidden = rtl_anchor(hidden_raw, lang)

    lines = [_escape_html(title), "", _escape_html(preview)]
    if hidden:
        lines += ["", f"<blockquote expandable>{_escape_html(hidden)}</blockquote>"]
    lines += ["", f"🔗 {_escape_html(item['url'])}"]
    return "\n".join(lines)


async def _send(bot: Bot, chat_id: str, text: str, *, disable_preview: bool,
                 parse_mode: str | None = None) -> tuple[bool, int | None]:
    try:
        message = await bot.send_message(
            chat_id=chat_id,
            text=text,
            disable_web_page_preview=disable_preview,
            parse_mode=parse_mode,
        )
        return True, message.message_id
    except TelegramError:
        logger.warning("Failed to send Telegram message to %s", chat_id, exc_info=True)
        return False, None


def _bots_for(channels: list[Channel]) -> dict[str, Bot]:
    """One Bot per distinct token, reused across all items in the run."""
    bots: dict[str, Bot] = {}
    for ch in channels:
        if ch.configured and ch.token not in bots:
            bots[ch.token] = Bot(token=ch.token)
    return bots


async def _publish_all(items: list[dict]) -> list[dict[str, tuple[bool, int | None]]]:
    channels = [_channel(lang) for lang in LANGS]
    bots = _bots_for(channels)
    results = []
    for i, item in enumerate(items):
        if i > 0:
            await asyncio.sleep(config.TELEGRAM_SEND_DELAY_SECONDS)
        per_lang: dict[str, tuple[bool, int | None]] = {}
        for ch in channels:
            if not ch.configured:
                per_lang[ch.lang] = (False, None)
                continue
            text = format_post(item, ch.lang)
            per_lang[ch.lang] = await _send(bots[ch.token], ch.chat_id, text,
                                             disable_preview=False, parse_mode="HTML")
        results.append(per_lang)
    return results


def publish_items(items: list[dict]) -> list[tuple[dict, dict[str, tuple[bool, int | None]]]]:
    """Send each item to every configured channel, in order.

    Returns one (item, results) pair per item, where results maps each language
    to a (success, message_id) tuple, e.g.
        {"en": (True, 4021), "fa": (True, 88)}
    Unconfigured channels report (False, None)."""
    if not items:
        return []
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHANNEL_ID:
        logger.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHANNEL_ID not set - skipping publish")
        skipped = {lang: (False, None) for lang in LANGS}
        return [(item, dict(skipped)) for item in items]

    results = asyncio.run(_publish_all(items))
    return list(zip(items, results))


def send_text(text: str, lang: str = "en") -> int | None:
    """Send a standalone message (e.g. a digest) to one channel. Returns the
    message id on success, else None (including when that channel isn't set up)."""
    ch = _channel(lang)
    if not ch.configured:
        logger.warning("%s channel not configured - skipping text send", ch.lang)
        return None
    bot = Bot(token=ch.token)
    _, message_id = asyncio.run(_send(bot, ch.chat_id, text, disable_preview=True))
    return message_id
