"""Orchestrator. Runs once per invocation - triggered on a GitHub Actions cron,
not a persistent process, since this is stateless polling.

Flow: fetch -> dedupe -> classify+write (DeepSeek) -> select within daily cap
-> enqueue (post_history.json "_queue") -> if this run lands in one of
config.PUBLISH_HOURS, drain a small batch from the queue and publish it
(Telegram) -> maybe post daily/weekly digests (see digest.py) -> save updated
post_history.json (committed back by the workflow).

Fetching/classifying is decoupled from publishing on purpose: news breaks in
bursts, but we still want the day's posts spread across a handful of fixed
times rather than all going out the moment they're approved.
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


def _current_publish_slot() -> str | None:
    """The current hour's slot key (e.g. "2026-07-26-13") if local time falls
    on one of config.PUBLISH_HOURS, else None."""
    now_local = datetime.now(ZoneInfo(config.POSTING_TIMEZONE))
    if now_local.hour not in config.PUBLISH_HOURS:
        return None
    return now_local.strftime("%Y-%m-%d-%H")


def publish_queue(history: dict, force: bool = False) -> None:
    """Drain up to config.PUBLISH_BATCH_SIZE items from the publish queue, but
    only during a config.PUBLISH_HOURS slot, and at most once per slot (a
    manual re-run or cron overlap within the same hour is a no-op past the
    first). Items whose send fails are left at the front of the queue to retry
    next slot rather than being dropped or recounted against the daily cap.
    `force` (same flag as the posting-window bypass) skips both the slot-hour
    and once-per-slot checks, for manually testing a real send."""
    slot = _current_publish_slot()
    if slot is None:
        if not force:
            return
    else:
        publish_state = dedupe.get_publish_state(history)
        if publish_state.get("last_slot") == slot and not force:
            return
        publish_state["last_slot"] = slot
    slot_label = slot or "forced"

    queue = dedupe.get_queue(history)
    if not queue:
        logger.info("Publish slot %s: queue is empty", slot_label)
        return

    batch = queue[:config.PUBLISH_BATCH_SIZE]
    rest = queue[config.PUBLISH_BATCH_SIZE:]
    results = publish.publish_items(batch)

    sent = 0
    retry = []
    for item, per_lang in results:
        en_ok, en_message_id = per_lang.get("en", (False, None))
        fa_ok, fa_message_id = per_lang.get("fa", (False, None))
        if en_ok:
            dedupe.mark_posted(history, item, en_message_id, fa_message_id)
            sent += 1
        else:
            retry.append(item)
    history["_queue"] = retry + rest
    logger.info("Publish slot %s: sent %d/%d item(s), %d remaining in queue",
                slot_label, sent, len(batch), len(history["_queue"]))


def _sort_key(item: dict):
    try:
        published_dt = datetime.fromisoformat(item["published"])
    except ValueError:
        published_dt = datetime.min.replace(tzinfo=timezone.utc)
    return (classify_and_write.PRIORITY_RANK.get(item["priority"], 2), -published_dt.timestamp())


def select_for_publishing(classified: list[dict], meta: dict) -> list[dict]:
    """Pick the best candidates within the daily cap (config.MAX_POSTS_PER_DAY),
    with a stricter sub-cap on the "courses" category. Priority high > medium > low,
    newest first as a tiebreaker."""
    candidates = [c for c in classified if c["relevant"] and not c.get("duplicate_of")]
    candidates.sort(key=_sort_key)

    remaining_total = config.MAX_POSTS_PER_DAY - meta["posts_today"]
    remaining_course = config.MAX_COURSE_POSTS_PER_DAY - meta["posts_today_by_category"].get("courses", 0)

    selected = []
    for item in candidates:
        if remaining_total <= 0:
            break
        if item["category"] == "courses":
            if remaining_course <= 0:
                continue
            remaining_course -= 1
        selected.append(item)
        remaining_total -= 1
    return selected


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

        meta = dedupe.get_meta(history)
        to_publish = select_for_publishing(classified, meta)
        to_publish_ids = {id(item) for item in to_publish}

        if dry_run:
            logger.info("[dry-run] Would queue %d item(s) for publishing:", len(to_publish))
            for item in to_publish:
                logger.info("[dry-run]   (%s/%s) %s -- %s", item["category"], item["priority"],
                            item.get("post_title") or item["title"], item["url"])
            # Bookkeeping only (prevents reclassifying the same items later) -
            # a dry run must not reserve daily-cap slots or touch the queue.
            for item in classified:
                dedupe.add_history_entry(history, item, posted=False)
        else:
            # Every classified item gets a history entry now (so high-volume
            # feeds aren't reclassified while a selected item sits in queue);
            # only the selected ones are enqueued and reserve a cap slot -
            # actually reaching Telegram happens later in publish_queue().
            for item in classified:
                dedupe.add_history_entry(history, item, posted=False)
            for item in to_publish:
                dedupe.enqueue_for_publish(history, item)
                meta["posts_today"] += 1
                meta["posts_today_by_category"][item["category"]] = (
                    meta["posts_today_by_category"].get(item["category"], 0) + 1
                )

            logger.info("Run complete: %d classified, %d queued for publishing (queue depth %d)",
                        len(classified), len(to_publish_ids), len(dedupe.get_queue(history)))

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
