"""Publish layer: send approved items to the Telegram channel via the Bot API."""

from __future__ import annotations

import asyncio
import logging
import re

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


def _with_leading_emoji(text: str, category: str) -> str:
    text = (text or "").strip()
    if text and _EMOJI_START_RE.match(text):
        return text
    emoji = config.CATEGORY_EMOJIS.get(category, config.DEFAULT_EMOJI)
    return f"{emoji} {text}".strip()


def format_post(item: dict) -> str:
    title = _with_leading_emoji(item["post_title"], item["category"])
    body = _with_leading_emoji(item["post_body"], item["category"])
    return f"{title}\n\n{body}\n\n🔗 {item['url']}"


async def _send_one(bot: Bot, item: dict) -> tuple[bool, int | None]:
    try:
        message = await bot.send_message(
            chat_id=config.TELEGRAM_CHANNEL_ID,
            text=format_post(item),
            disable_web_page_preview=False,
        )
        return True, message.message_id
    except TelegramError:
        logger.warning("Failed to send Telegram message for '%s'", item["title"], exc_info=True)
        return False, None


async def _publish_all(items: list[dict]) -> list[tuple[bool, int | None]]:
    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    results = []
    for i, item in enumerate(items):
        if i > 0:
            await asyncio.sleep(config.TELEGRAM_SEND_DELAY_SECONDS)
        results.append(await _send_one(bot, item))
    return results


def publish_items(items: list[dict]) -> list[tuple[dict, bool, int | None]]:
    """Send each item to the Telegram channel in order. Returns (item, success, message_id) triples."""
    if not items:
        return []
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHANNEL_ID:
        logger.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHANNEL_ID not set - skipping publish")
        return [(item, False, None) for item in items]

    results = asyncio.run(_publish_all(items))
    return [(item, ok, message_id) for item, (ok, message_id) in zip(items, results)]


async def _send_text(bot: Bot, text: str) -> int | None:
    try:
        message = await bot.send_message(
            chat_id=config.TELEGRAM_CHANNEL_ID,
            text=text,
            disable_web_page_preview=True,
        )
        return message.message_id
    except TelegramError:
        logger.warning("Failed to send digest message", exc_info=True)
        return None


def send_text(text: str) -> int | None:
    """Send a standalone message (e.g. a digest) not tied to a fetched item."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHANNEL_ID:
        logger.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHANNEL_ID not set - skipping digest send")
        return None
    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    return asyncio.run(_send_text(bot, text))
