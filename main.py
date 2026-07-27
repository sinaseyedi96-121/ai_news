"""Orchestrator. Runs once per invocation - triggered on a GitHub Actions cron,
not a persistent process, since this is stateless polling.

Flow: fetch -> dedupe -> classify+write (DeepSeek) -> enqueue every relevant
item (post_history.json "_queue"), keeping only the freshest MAX_QUEUE_DEPTH ->
if a config.PUBLISH_HOURS slot has passed and hasn't been served yet, send the
freshest queued item (Telegram), charging the daily cap here -> maybe post
daily/weekly digests (see digest.py) -> save updated post_history.json
(committed back by the workflow).

Fetching/classifying is decoupled from publishing on purpose: news breaks in
bursts, but we still want the day's posts spread across fixed times, freshest
first, rather than all going out the moment they're approved. Because the
GitHub Actions cron is unreliable (late/dropped runs), a slot is served by the
first run *after* its hour, not only by a run landing exactly on it.
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import classify_and_write
import config
import dedupe
import digest
import fetch
import publish

logger = logging.getLogger(__name__)


def _within_posting_window() -> bool:
    """True during the channel's local daytime hours (config.POSTING_TIMEZONE).
    The GitHub Actions cron only covers a superset of this in UTC (a fixed cron
    can't track DST) - this check is the precise, DST-safe gate."""
    now_local = datetime.now(ZoneInfo(config.POSTING_TIMEZONE))
    return config.POSTING_WINDOW_START_HOUR <= now_local.hour < config.POSTING_WINDOW_END_HOUR


def _due_publish_slot() -> str | None:
    """Slot key (e.g. "2026-07-26-13") for the most recent config.PUBLISH_HOURS
    hour that has already passed today, or None if none has yet.

    Keying off "most recent slot that has passed" rather than "the clock is
    exactly on a slot hour" is what makes publishing robust to GitHub Actions'
    unreliable cron: a slot whose on-the-hour run was late or dropped still gets
    served by the next run that comes along. publish_queue's once-per-slot guard
    keeps each slot to a single batch, so this never double-posts a slot."""
    now_local = datetime.now(ZoneInfo(config.POSTING_TIMEZONE))
    passed = [h for h in config.PUBLISH_HOURS if h <= now_local.hour]
    if not passed:
        return None
    return f"{now_local:%Y-%m-%d}-{max(passed):02d}"


def _sort_key(item: dict):
    try:
        published_dt = datetime.fromisoformat(item["published"])
    except ValueError:
        published_dt = datetime.min.replace(tzinfo=timezone.utc)
    return (classify_and_write.PRIORITY_RANK.get(item["priority"], 2), -published_dt.timestamp())


def _trim_queue(history: dict) -> None:
    """Keep the queue sorted best-first (priority, then newest) and capped at
    config.MAX_QUEUE_DEPTH, dropping the stalest overflow. This is what lets
    fresh news reach the channel even after a busy burst: the cap is spent at
    send time, not on enqueue, so the queue always holds the freshest
    candidates rather than whatever happened to be classified first."""
    queue = dedupe.get_queue(history)
    queue.sort(key=_sort_key)
    if len(queue) > config.MAX_QUEUE_DEPTH:
        dropped = len(queue) - config.MAX_QUEUE_DEPTH
        del queue[config.MAX_QUEUE_DEPTH:]
        logger.info("Trimmed %d stale item(s) beyond queue depth cap %d", dropped, config.MAX_QUEUE_DEPTH)


def _pick_sendable(queue: list[dict], meta: dict, limit: int) -> list[dict]:
    """The next `limit` queue items that fit the daily caps (total
    config.MAX_POSTS_PER_DAY and the stricter courses sub-cap), counted against
    what's *actually been posted today* (meta) - so a day that started full
    can't be re-filled, and courses stay rare. Returns references into `queue`."""
    picked: list[dict] = []
    room = config.MAX_POSTS_PER_DAY - meta["posts_today"]
    course_used = meta["posts_today_by_category"].get("courses", 0)
    for item in queue:
        if len(picked) >= limit or room <= 0:
            break
        if item["category"] == "courses" and course_used >= config.MAX_COURSE_POSTS_PER_DAY:
            continue
        picked.append(item)
        room -= 1
        if item["category"] == "courses":
            course_used += 1
    return picked


def publish_queue(history: dict, force: bool = False) -> None:
    """Send the freshest queued item(s) for the current slot. Runs at most once
    per config.PUBLISH_HOURS slot (a re-run or extra cron within the same slot
    is a no-op), and only for a slot that has already passed today - see
    _due_publish_slot for why "passed", not "exactly now". The daily cap is
    charged here, at send time; items whose send fails stay in the queue to
    retry (and the slot isn't marked served, so the next run retries it too).
    `force` bypasses the passed-slot and once-per-slot checks for manual tests."""
    slot = _due_publish_slot()
    if slot is None and not force:
        return
    publish_state = dedupe.get_publish_state(history)
    if slot is not None and publish_state.get("last_slot") == slot and not force:
        return
    slot_label = slot or "forced"

    meta = dedupe.get_meta(history)
    queue = dedupe.get_queue(history)
    queue.sort(key=_sort_key)

    batch = _pick_sendable(queue, meta, config.PUBLISH_BATCH_SIZE)
    if not batch:
        # Empty queue or caps already spent for today - mark the slot served so
        # we don't reconsider it every run, and move on.
        if slot is not None:
            publish_state["last_slot"] = slot
        logger.info("Publish slot %s: nothing to send (queue depth %d, posts_today %d)",
                    slot_label, len(queue), meta["posts_today"])
        return

    results = publish.publish_items(batch)

    sent_ids: set[int] = set()
    for item, per_lang in results:
        en_ok, en_message_id = per_lang.get("en", (False, None))
        _, fa_message_id = per_lang.get("fa", (False, None))
        if en_ok:
            dedupe.mark_posted(history, item, en_message_id, fa_message_id)
            meta["posts_today"] += 1
            meta["posts_today_by_category"][item["category"]] = (
                meta["posts_today_by_category"].get(item["category"], 0) + 1
            )
            sent_ids.add(id(item))

    history["_queue"] = [i for i in queue if id(i) not in sent_ids]
    if sent_ids and slot is not None:
        publish_state["last_slot"] = slot
    logger.info("Publish slot %s: sent %d/%d item(s), %d remaining in queue, posts_today %d",
                slot_label, len(sent_ids), len(batch), len(history["_queue"]), meta["posts_today"])


def run(dry_run: bool = False, force: bool = False) -> None:
    if not dry_run and not force and not _within_posting_window():
        logger.info(
            "Outside the daytime posting window (%s, local hours %d-%d) - skipping this run",
            config.POSTING_TIMEZONE, config.POSTING_WINDOW_START_HOUR, config.POSTING_WINDOW_END_HOUR,
        )
        return

    history = dedupe.load_history()

    raw_items = fetch.fetch_all()
    logger.info("Fetched %d raw items total", len(raw_items))

    new_items = dedupe.filter_new_items(raw_items, history)
    if not new_items:
        logger.info("Nothing new this run")
    else:
        classified = classify_and_write.classify_items(new_items)
        relevant = [c for c in classified if c["relevant"] and not c.get("duplicate_of")]

        if dry_run:
            logger.info("[dry-run] Would enqueue %d relevant item(s):", len(relevant))
            for item in relevant:
                logger.info("[dry-run]   (%s/%s) %s -- %s", item["category"], item["priority"],
                            item.get("post_title") or item["title"], item["url"])
            # Bookkeeping only (prevents reclassifying the same items later) -
            # a dry run must not touch the queue.
            for item in classified:
                dedupe.add_history_entry(history, item, posted=False)
        else:
            # Every classified item gets a history entry now (so high-volume
            # feeds aren't reclassified while an item waits in the queue); every
            # relevant one is enqueued. The daily cap isn't touched here - it's
            # charged at send time in publish_queue - and _trim_queue keeps only
            # the freshest MAX_QUEUE_DEPTH candidates so stale items don't linger.
            for item in classified:
                dedupe.add_history_entry(history, item, posted=False)
            for item in relevant:
                dedupe.enqueue_for_publish(history, item)
            _trim_queue(history)

            logger.info("Run complete: %d classified, %d relevant enqueued (queue depth %d)",
                        len(classified), len(relevant), len(dedupe.get_queue(history)))

    # Publishing and digests are time-triggered rather than tied to fresh
    # news, so both are checked on every non-dry-run invocation regardless of
    # whether anything new was fetched this hour.
    if not dry_run:
        publish_queue(history, force=force)
        digest.maybe_post_daily_digest(history)
        digest.maybe_post_weekly_digest(history)

    dedupe.save_history(history)


if __name__ == "__main__":
    logging.basicConfig(level=getattr(logging, config.LOG_LEVEL, logging.INFO),
                         format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Fetch, filter, and post AI news to Telegram")
    parser.add_argument("--dry-run", action="store_true",
                         help="Run the full pipeline but only log what would be posted, don't call Telegram")
    parser.add_argument("--force", action="store_true",
                         help="Bypass the daytime posting window check (for manual/test runs)")
    args = parser.parse_args()
    run(dry_run=args.dry_run, force=args.force)
