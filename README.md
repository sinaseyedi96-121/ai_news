# ai_news

A pipeline that fetches AI news from a curated set of sources, filters/dedupes it,
uses DeepSeek to classify and write short posts, and publishes them to a Telegram
channel. Runs hourly via GitHub Actions.

## Architecture

```
fetch.py               -> pulls raw items from sources.json (RSS) + NewsAPI (policy)
dedupe.py               -> drops items already seen (post_history.json)
classify_and_write.py   -> DeepSeek: filter relevance + write the Telegram post
publish.py              -> sends approved posts to the Telegram channel
main.py                 -> ties it all together, run once per invocation
```

## Required secrets

Set these as GitHub repo secrets (Settings -> Secrets and variables -> Actions)
so the workflow can inject them as env vars. Locally, copy `.env.example` to
`.env` and fill them in (`.env` is gitignored).

| Variable | Used for |
|---|---|
| `DEEPSEEK_API_KEY` | classify_and_write.py -> DeepSeek chat completions |
| `TELEGRAM_BOT_TOKEN` | publish.py -> Telegram Bot API |
| `TELEGRAM_CHANNEL_ID` | publish.py -> which channel to post to (e.g. `@your_channel` or a numeric chat id) |
| `NEWSAPI_KEY` | fetch.py -> AI policy/politics keyword search (free "Developer" tier: 100 req/day) |

The bot account behind `TELEGRAM_BOT_TOKEN` must be added to the channel as an
admin with "Post messages" permission.

## Adding or removing a source

- **RSS feed**: edit `sources.json`, add/remove a `{"name": ..., "url": ...}`
  entry under the relevant category key. No code changes needed - `fetch.py`
  iterates whatever's in that file. Test the feed first (see below) before
  committing it, especially anything under `unofficial_lab_mirrors_VERIFY_FIRST`
  - those are third-party mirrors, not official feeds, and can go stale or
    disappear without notice.
- **NewsAPI query/domains** (AI policy category): edit `NEWSAPI_QUERY` /
  `NEWSAPI_DOMAINS` in `config.py`.
- **Categories**: the top-level keys in `sources.json` are just labels passed
  through to DeepSeek as context - add a new one if you add a genuinely new
  kind of source.

## How dedup works

Two layers, both checked against `post_history.json`:

1. **URL match** - each URL is canonicalized (tracking params like `utm_*`
   stripped, scheme/host normalized, trailing slash dropped) and hashed. Exact
   match against a previously-seen hash = duplicate.
2. **Title similarity** - titles are normalized and compared (rapidfuzz
   `token_sort_ratio`, threshold 90) against titles seen in the last 7 days.
   This catches the same story covered by two different outlets under two
   different URLs.

Every item that reaches the classify stage gets a `post_history.json` entry
the moment it's classified - whether DeepSeek accepts or rejects it - so
high-volume feeds (arXiv, Hacker News) don't get reclassified every run.
Entries older than 30 days are pruned on every save so the file doesn't grow
unbounded.

`post_history.json` also holds a `_meta` block tracking the daily posting cap:

```json
"_meta": {"date": "2026-07-21", "posts_today": 3, "posts_today_by_category": {"policy": 1}}
```

At most `MAX_POSTS_PER_DAY` (8, in `config.py`) items get posted per UTC day,
selected by priority (high > medium > low, newest first as a tiebreaker) across
all runs that day. `courses` is further capped at `MAX_COURSE_POSTS_PER_DAY` (1)
since course/certification announcements are explicitly lower priority. Items
that lose out to the cap are recorded as seen-but-not-posted and won't be
retried later that day - the cap is a hard editorial limit on channel volume,
not a delayed queue.

## Testing a single feed locally before adding it to sources.json

```bash
pip install -r requirements.txt
python fetch.py --test-feed "https://example.com/feed.xml"
```

Prints the item count and the first 10 titles/URLs it parsed. If it fails,
`feedparser` usually still returns something readable in the error log - if it
returns 0 items even though the feed URL loads in a browser, check whether the
site is blocking non-browser user agents or whether the URL needs to change
(e.g. some sites gate their real feed behind a redirect).

## Running the full pipeline locally

```bash
python main.py --dry-run
```

`--dry-run` runs fetch -> dedupe -> classify normally but logs what *would*
be posted instead of calling the Telegram API - useful for checking the
pipeline end-to-end before secrets are wired up, or before trusting it with
a live channel.

## Notes

- Every RSS fetch is wrapped in its own try/except (see `fetch.fetch_rss_feed`)
  - one broken feed logs a warning and returns no items, it never kills the run.
- NewsAPI's free tier only covers roughly the last month of articles and caps
  at 100 requests/day; at one query per hourly run that's 24 requests/day, well
  under the limit.
- DeepSeek calls use the `deepseek-chat` model (the cheaper, non-reasoning
  tier) - this is high-volume, low-reasoning classification/formatting work,
  not something that needs `deepseek-reasoner`.
