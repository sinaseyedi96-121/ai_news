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

# --- DeepSeek ------------------------------------------------------------
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"  # cheaper non-reasoning tier; plenty for classify+write
DEEPSEEK_TEMPERATURE = 0.3
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
