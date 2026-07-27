# ai_news

A pipeline that fetches AI news from a curated set of sources, filters/dedupes it,
uses DeepSeek to classify and write short posts, and publishes them to a Telegram
channel. Runs every 15 minutes during daytime hours via GitHub Actions.

DeepSeek writes each post in English **and** Farsi, so an optional second channel
(the Farsi mirror) receives a translated copy of every post and digest. Leave the
`TELEGRAM_FARSI_*` secrets blank to run English-only.

## Architecture

```
fetch.py               -> pulls raw items from sources.json (RSS) + NewsAPI (policy)
dedupe.py               -> drops items already seen (post_history.json), holds the publish queue
classify_and_write.py   -> DeepSeek: filter relevance + write the post (EN + FA)
publish.py              -> sends approved posts to the Telegram channel(s)
digest.py               -> builds/sends the daily top-3 and weekly top-5 recaps (per channel)
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
| `TELEGRAM_CHANNEL_USERNAME` | digest.py -> public `@handle` (no `@`) used to build `https://t.me/<handle>/<message_id>` links in digest posts. Optional - only needed if `TELEGRAM_CHANNEL_ID` isn't already an `@handle` |
| `TELEGRAM_FARSI_BOT_TOKEN` | publish.py -> Bot API token for the Farsi mirror channel. **Optional** - leave blank to disable Farsi posting |
| `TELEGRAM_FARSI_CHANNEL_ID` | publish.py -> which Farsi channel to post to (e.g. `@ai_news_247_farsi` or a numeric chat id). Optional (see above) |
| `TELEGRAM_FARSI_CHANNEL_USERNAME` | digest.py -> Farsi channel `@handle` for digest back-links. Optional - defaults to `TELEGRAM_FARSI_CHANNEL_ID` when that's an `@handle` |
| `NEWSAPI_KEY` | fetch.py -> AI policy/politics keyword search (free "Developer" tier: 100 req/day) |

Each bot account (`TELEGRAM_BOT_TOKEN`, and `TELEGRAM_FARSI_BOT_TOKEN` if used)
must be added to its channel as an admin with "Post messages" permission. Farsi
posting only kicks in when **both** `TELEGRAM_FARSI_BOT_TOKEN` and
`TELEGRAM_FARSI_CHANNEL_ID` are set; otherwise the pipeline runs English-only.

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

At most `MAX_POSTS_PER_DAY` (8, in `config.py`) items are *sent* per UTC day.
The cap is charged when an item is actually posted (not when it's queued), so a
busy morning can't reserve the whole day's quota and then block genuinely fresh
afternoon news - see "Publish cadence" below. `courses` is further capped at
`MAX_COURSE_POSTS_PER_DAY` (1) since course/certification announcements are
explicitly lower priority. Every relevant item is enqueued; the queue is held
to the freshest `MAX_QUEUE_DEPTH` (12) candidates by priority (high > medium >
low, newest first as a tiebreaker), and the rest are recorded as
seen-but-not-posted. Items go out on the schedule described below.

## Post format

DeepSeek writes each post as a headline + explanation, not one flat paragraph:

- `post_title` - a punchy, emoji-led headline (e.g. `🚀 OpenAI ships GPT-5.5`).
- `post_body` - 4-6 sentences of plain-text explanation (what happened, who's
  involved, key numbers, why it matters), starting with a different emoji and
  weaving in 2-3 more near key facts.

DeepSeek also returns `post_title_fa` / `post_body_fa` - fluent Farsi
translations that keep the same emojis and product names - used for the Farsi
mirror channel. `publish.format_post(item, lang)` joins the chosen language's
fields as `title\n\nbody\n\n🔗 url` (Farsi falls back to the English fields if a
translation is missing). If DeepSeek ever omits the leading emoji,
`publish._with_leading_emoji` falls back to a per-category emoji from
`config.CATEGORY_EMOJIS` so every post still gets one.

Each selected item is sent to every configured channel in the same run, and its
`post_history.json` entry records a per-channel message id (`message_id` for
English, `message_id_fa` for Farsi) so each channel's digest links to its own
posts.

## Posting window

Posts only go out during local daytime hours, `config.POSTING_WINDOW_START_HOUR`
-`config.POSTING_WINDOW_END_HOUR` in `config.POSTING_TIMEZONE` (currently
8:00-23:00 `Europe/Berlin`). `main._within_posting_window()` checks this with
`zoneinfo`, which tracks CET/CEST DST automatically - this is the precise gate.

The GitHub Actions cron (`.github/workflows/post.yml`) only restricts runs to a
UTC superset of that window (every 15 min, `06:00-22:59 UTC`); a fixed cron
can't track DST on its own, so the exact cutoff is always decided in code, not
by the cron expression. Outside the window, `main.run()` returns immediately
without fetching, classifying, or posting anything.

To bypass the window (e.g. testing manually outside daytime hours), either pass
`--force` to `main.py` or trigger the workflow via `workflow_dispatch` with the
`force` input set to `true`.

## Publish cadence: queue + evenly-spaced daily slots

Fetching/classifying and actually sending to Telegram are decoupled on
purpose. News breaks in bursts (a busy morning can produce many items at once),
but we still want the day's posts spread evenly across the day, freshest first,
instead of all landing in the same run.

- Every run that finds new, relevant items appends them to a publish queue
  (`post_history.json`'s `_queue` list) instead of sending them, then trims the
  queue to the freshest `MAX_QUEUE_DEPTH` (12) candidates.
- Separately, every run asks `main._due_publish_slot()` for the most recent
  `config.PUBLISH_HOURS` slot (local `config.POSTING_TIMEZONE`, currently `8,
  10, 12, 14, 16, 18, 20, 22`) that has **already passed** today. If that slot
  hasn't been served yet, `main.publish_queue()` sends the freshest
  `config.PUBLISH_BATCH_SIZE` (1) item to every configured channel, charging
  the daily cap. 8 slots x 1 post = `MAX_POSTS_PER_DAY` on a full day.
- Keying off "most recent slot that has passed" (not "the clock is exactly on a
  slot hour") is deliberate: GitHub Actions' scheduled cron is routinely late
  and sometimes drops runs entirely, so a slot whose on-the-hour run never
  happened is still picked up by the next run that comes along. The 15-minute
  cron keeps that catch-up tight.
- Each slot only fires once (tracked in `post_history.json`'s `_publish_state`
  block), so extra runs within the same slot are a no-op. `--force` bypasses
  both the passed-slot check and the once-per-slot guard, for manually testing
  a real send.
- An item whose send fails (e.g. a transient Telegram error) stays in the queue
  and the slot is left unserved, so the very next run retries it rather than
  waiting for the next slot - and nothing is double-counted against the cap.

This was a deliberate choice over trying to have Telegram itself hold and send
messages later: the Bot API has no "send at a future time" parameter for bots
- scheduled sending is a client-only feature of regular user accounts in the
Telegram app, not something a bot account can invoke. A self-managed queue is
also the better foundation if another platform (X, LinkedIn, ...) gets added
later - it becomes another `publish`-style adapter that the same
`publish_queue()` scheduler calls, rather than reimplementing pacing logic per
platform.

## Digest posts

On top of the regular news posts, two recap posts are built from
`post_history.json`'s posted entries and sent to each configured channel (an
English recap to the English channel, a Farsi recap to the Farsi channel):

- **Daily top-3** - the day's best posts (by priority, newest first as a
  tiebreaker), fires once per local day at/after `config.DAILY_DIGEST_HOUR`
  (21:00). Skipped if fewer than `DAILY_DIGEST_MIN_ITEMS` (2) were posted that
  day.
- **Weekly top-5** - the best posts over the trailing 7 days, fires once per
  week on `config.WEEKLY_DIGEST_WEEKDAY` (Sunday) at/after
  `WEEKLY_DIGEST_HOUR` (20:00). Skipped if fewer than `WEEKLY_DIGEST_MIN_ITEMS`
  (3) were posted that week.

Both link back to the channel's **own Telegram posts**
(`https://t.me/<channel username>/<message_id>`), not the original news URLs -
the point is to resurface top stories inside the channel, not send readers
elsewhere. This needs the channel's `..._USERNAME` set (see above); if it isn't,
digest entries fall back to a plain title line with no link. The Farsi digest
uses the Farsi title (`post_title_fa`) and only includes items that were
actually posted to the Farsi channel (i.e. have a `message_id_fa`).

Since these are time-triggered rather than tied to fresh news, `main.run()`
checks them on every non-dry-run invocation, even when nothing new was fetched
that hour. Idempotency (only firing once per day/week, per language) is tracked
in `post_history.json`'s `_digest_state` block, which - unlike `_meta` -
persists across `ensure_today`'s daily reset. The `_fa` keys track the Farsi
mirror's digests independently:

```json
"_digest_state": {"daily_digest_date": "2026-07-23", "weekly_digest_date": "2026-07-20", "daily_digest_date_fa": "2026-07-23", "weekly_digest_date_fa": "2026-07-20"}
```

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
a live channel. `--dry-run` always bypasses the posting window; add `--force`
too if you want to test a real (non-dry-run) post outside daytime hours.

## Notes

- Every RSS fetch is wrapped in its own try/except (see `fetch.fetch_rss_feed`)
  - one broken feed logs a warning and returns no items, it never kills the run.
- NewsAPI's free tier only covers roughly the last month of articles and caps
  at 100 requests/day; at one query per run every 15 min across the 06:00-22:59
  UTC window that's ~68 requests/day, still under the limit.
- DeepSeek calls use the `deepseek-chat` model (the cheaper, non-reasoning
  tier) - this is high-volume, low-reasoning classification/formatting work,
  not something that needs `deepseek-reasoner`.
