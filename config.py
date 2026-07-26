"""All constants and secret-loading for the AI News bot. No magic values in other modules."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

# --- Paths -------------------------------------------------------------
SOURCES_PATH = BASE_DIR / "sources.json"
POST_HISTORY_PATH = BASE_DIR / "post_history.json"

# --- Secrets (env vars only, see .env.example) --------------------------
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")
NEWSAPI_KEY = os.environ.get("NEWSAPI_KEY", "")

# Public @handle (no "@") for building https://t.me/<handle>/<message_id>
# links in digest posts. Defaults to TELEGRAM_CHANNEL_ID when that's already
# an "@handle" (the common case); set explicitly if the channel is sent to by
# numeric chat id but still has a public username.
TELEGRAM_CHANNEL_USERNAME = os.environ.get("TELEGRAM_CHANNEL_USERNAME") or (
    TELEGRAM_CHANNEL_ID.lstrip("@") if TELEGRAM_CHANNEL_ID.startswith("@") else ""
)

# --- Farsi mirror channel (optional) -----------------------------------
# A second channel that receives a Farsi translation of every post/digest,
# via its own bot. Left empty -> Farsi posting is silently skipped and the
# pipeline behaves exactly as the English-only version. Same @handle vs
# numeric-chat-id rules as the English channel above.
TELEGRAM_FARSI_BOT_TOKEN = os.environ.get("TELEGRAM_FARSI_BOT_TOKEN", "")
TELEGRAM_FARSI_CHANNEL_ID = os.environ.get("TELEGRAM_FARSI_CHANNEL_ID", "")
TELEGRAM_FARSI_CHANNEL_USERNAME = os.environ.get("TELEGRAM_FARSI_CHANNEL_USERNAME") or (
    TELEGRAM_FARSI_CHANNEL_ID.lstrip("@") if TELEGRAM_FARSI_CHANNEL_ID.startswith("@") else ""
)

# --- DeepSeek ------------------------------------------------------------
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-v4-flash"  # cheaper tier; plenty for classify+write ("deepseek-chat" was retired)
DEEPSEEK_TEMPERATURE = 0.3
# v4-flash reasons over every item before writing EN+FA post bodies, which is much
# slower than the old non-reasoning deepseek-chat - a full 30-item batch with several
# relevant stories can take well over REQUEST_TIMEOUT_SECONDS (20s, tuned for plain
# GET calls elsewhere), so give it its own generous ceiling.
DEEPSEEK_TIMEOUT_SECONDS = 90
MAX_ITEMS_PER_CLASSIFY_BATCH = 30  # chunk size sent to DeepSeek per call
CLASSIFY_SUMMARY_CHARS = 800  # source summary chars fed to DeepSeek as writing material

# --- NewsAPI (free "Developer" tier: 100 req/day, ~last 30 days of articles) ---
NEWSAPI_URL = "https://newsapi.org/v2/everything"
NEWSAPI_QUERY = '"AI regulation" OR "AI Act" OR "AI policy" OR "AI legislation" OR "AI export controls"'
NEWSAPI_DOMAINS = "reuters.com,politico.com,axios.com"
NEWSAPI_LOOKBACK_HOURS = 24  # wider than the poll interval as a safety margin; dedup handles overlap

# --- Dedup ---------------------------------------------------------------
HISTORY_RETENTION_DAYS = 30       # prune history entries older than this on every save
TITLE_DEDUPE_WINDOW_DAYS = 7      # only compare titles against entries within this window
TITLE_DEDUPE_THRESHOLD = 90       # rapidfuzz token_sort_ratio, 0-100

# --- Posting caps ----------------------------------------------------------
MAX_POSTS_PER_DAY = 8             # hard cap across all categories, resets at UTC midnight
MAX_COURSE_POSTS_PER_DAY = 1      # courses/certifications are lower priority, post rarely
TELEGRAM_SEND_DELAY_SECONDS = 2   # small pause between sends in the same run

# --- Posting window --------------------------------------------------------
# Only post during local daytime hours; the GitHub Actions cron itself only
# covers a superset of this (see .github/workflows/post.yml) since a fixed
# UTC cron can't track DST, so this check (using the IANA tz database via
# zoneinfo) is the source of truth and handles CET/CEST automatically.
POSTING_TIMEZONE = "Europe/Berlin"
POSTING_WINDOW_START_HOUR = 8    # inclusive, local time
POSTING_WINDOW_END_HOUR = 23     # exclusive, local time

# --- Publish cadence -----------------------------------------------------
# Items that pass classification + the daily cap are queued (post_history.json
# "_queue") rather than sent immediately - main.publish_queue only drains the
# queue during these local hours (POSTING_TIMEZONE), up to PUBLISH_BATCH_SIZE
# items per slot, so the day's MAX_POSTS_PER_DAY posts land in evenly spaced
# bursts across the day instead of all at once whenever news happens to break.
# 4 slots x 2 posts = MAX_POSTS_PER_DAY on a full day.
PUBLISH_HOURS = (9, 13, 17, 21)
PUBLISH_BATCH_SIZE = 2

# --- Digests -----------------------------------------------------------
# Daily top-N recap, linking back to the channel's own posts (not the
# original news links) - fires once per local day at/after this hour.
DAILY_DIGEST_HOUR = 21           # local hour (POSTING_TIMEZONE)
DAILY_DIGEST_TOP_N = 3
DAILY_DIGEST_MIN_ITEMS = 2        # skip the recap on a very slow day

# Weekly top-N recap over the trailing 7 days - fires once per week, on/after
# this weekday+hour. Python weekday(): 0=Monday ... 6=Sunday.
WEEKLY_DIGEST_WEEKDAY = 6         # Sunday
WEEKLY_DIGEST_HOUR = 20           # local hour (POSTING_TIMEZONE)
WEEKLY_DIGEST_TOP_N = 5
WEEKLY_DIGEST_MIN_ITEMS = 3

# --- Formatting --------------------------------------------------------------
CATEGORY_EMOJIS = {
    "model_release": "🚀",
    "integration": "🤝",
    "infrastructure": "🏗️",
    "policy": "⚖️",
    "courses": "🎓",
    "research": "🔬",
    "other": "📰",
}
DEFAULT_EMOJI = "📰"

# --- Networking ------------------------------------------------------------
REQUEST_TIMEOUT_SECONDS = 20

# --- Logging -----------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
