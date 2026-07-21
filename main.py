"""Orchestrator. Runs once per invocation - triggered on a GitHub Actions cron,
not a persistent process, since this is stateless polling.

Flow: fetch -> dedupe -> classify+write (DeepSeek) -> select within daily cap
-> publish (Telegram) -> save updated post_history.json (committed back by
the workflow).
"""

import argparse
import logging
from datetime import datetime, timezone

import classify_and_write
import config
import dedupe
import fetch
import publish

logger = logging.getLogger(__name__)


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


def run(dry_run: bool = False) -> None:
    history = dedupe.load_history()

    raw_items = fetch.fetch_all()
    logger.info("Fetched %d raw items total", len(raw_items))

    new_items = dedupe.filter_new_items(raw_items, history)
    if not new_items:
        logger.info("Nothing new this run")
        dedupe.save_history(history)
        return

    classified = classify_and_write.classify_items(new_items)

    meta = dedupe.get_meta(history)
    to_publish = select_for_publishing(classified, meta)
    to_publish_ids = {id(item) for item in to_publish}

    if dry_run:
        logger.info("[dry-run] Would publish %d item(s):", len(to_publish))
        for item in to_publish:
            logger.info("[dry-run]   (%s/%s) %s -- %s", item["category"], item["priority"], item["title"], item["url"])
        results = [(item, False) for item in to_publish]
    else:
        results = publish.publish_items(to_publish)

    published_ok = {id(item) for item, ok in results if ok}

    for item in classified:
        posted = id(item) in published_ok
        dedupe.add_history_entry(history, item, posted=posted)
        if posted:
            meta["posts_today"] += 1
            meta["posts_today_by_category"][item["category"]] = (
                meta["posts_today_by_category"].get(item["category"], 0) + 1
            )

    dedupe.save_history(history)
    logger.info("Run complete: %d classified, %d selected, %d actually posted",
                len(classified), len(to_publish_ids), len(published_ok))


if __name__ == "__main__":
    logging.basicConfig(level=getattr(logging, config.LOG_LEVEL, logging.INFO),
                         format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Fetch, filter, and post AI news to Telegram")
    parser.add_argument("--dry-run", action="store_true",
                         help="Run the full pipeline but only log what would be posted, don't call Telegram")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
