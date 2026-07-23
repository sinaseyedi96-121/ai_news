"""Classify + write layer.

Sends surviving items (post-dedup) to DeepSeek in one batched call per chunk so
the model can see everything from this run together - that lets it catch
substance-duplicates (three outlets covering the same launch) that URL/title
matching in dedupe.py can't, by picking one canonical item per story and
marking the rest as duplicates.
"""

import json
import logging

import requests

import config

logger = logging.getLogger(__name__)

PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}

SYSTEM_PROMPT = """You are the editorial filter and writer for an AI news Telegram channel. You will
receive a numbered list of candidate items (title, source, published date, summary, url).
For EACH item, decide:

- relevant (bool): is this actually AI news in scope? In-scope categories:
  model/tool releases & product announcements; integrations between AI tools;
  data center / AI infrastructure (capacity, chip deals, openings/closures);
  AI policy/regulation/export controls/lawsuits/gov't initiatives; new AI
  courses/certifications from labs (LOW priority - see below); notable research
  that signals a genuinely new capability (NOT routine incremental arXiv papers).
- category: one of model_release | integration | infrastructure | policy |
  courses | research | other
- priority: high | medium | low
  - courses/certifications are ALWAYS low priority regardless of source quality
  - routine research, minor version bumps, opinion pieces are low
  - major model releases, big policy/regulatory news, large infra deals are high
- duplicate_of: null, or the index (integer) of another item in THIS list
  covering the same underlying event (pick the single best-sourced item as
  canonical; mark the rest as duplicates)
- reject_reason: short string, required if relevant=false or duplicate_of is set,
  otherwise null
- post_title and post_body: ONLY if relevant=true and duplicate_of=null (otherwise
  both null). Write a headline + explanation, not one flat paragraph:
  - post_title: a punchy headline, at most 12 words, no trailing period. MUST
    start with exactly one emoji that fits the story (e.g. 🚀 launches/releases,
    💰 funding/big-money deals, ⚖️ policy/legal/regulation, 🏗️ infrastructure/data
    centers/chips, 🔬 research breakthroughs, 🎓 courses/certifications, 🤝
    partnerships/integrations).
  - post_body: 4-6 sentences of plain-text explanation covering what happened,
    who's involved, key numbers/facts, and why it matters for the reader. MUST
    start with a different emoji than the title's, and should naturally weave in
    2-3 more emojis next to key facts, numbers, or outcomes (e.g. 📈 growth, 💵
    dollar figures, ⚡ speed/performance, 🌍 global reach, 🔥 notable/big news) to
    make it visually engaging - but don't force one into every sentence, and
    never use more than one emoji in a row. No markdown headers, no hashtags, no
    URLs (the code appends the link separately).

Community-signal items (Hacker News/Reddit) are only relevant if they themselves
report concrete news - general discussion/opinion threads are not relevant.

Respond with STRICT JSON only, no prose before or after, in this exact shape:
{"decisions": [{"index": 1, "relevant": true, "category": "model_release",
"priority": "high", "duplicate_of": null, "reject_reason": null,
"post_title": "...", "post_body": "..."}]}

Include exactly one decision object per input item, in the same order, with
"index" matching the item's number in the input list."""


def _build_user_prompt(batch: list[dict]) -> str:
    lines = []
    for i, item in enumerate(batch, start=1):
        summary = (item.get("summary") or "")[:config.CLASSIFY_SUMMARY_CHARS]
        lines.append(
            f"{i}. Title: {item['title']}\n"
            f"   Source: {item['source']} ({item['category']})\n"
            f"   Published: {item['published']}\n"
            f"   Summary: {summary}\n"
            f"   URL: {item['url']}"
        )
    return "\n\n".join(lines)


def call_deepseek(system_prompt: str, user_prompt: str) -> dict:
    if not config.DEEPSEEK_API_KEY:
        logger.warning("DEEPSEEK_API_KEY not set - skipping classification")
        return {"decisions": []}

    headers = {
        "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": config.DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": config.DEEPSEEK_TEMPERATURE,
        "response_format": {"type": "json_object"},
    }

    try:
        resp = requests.post(
            config.DEEPSEEK_API_URL, headers=headers, json=body,
            timeout=config.REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception:
        logger.warning("DeepSeek call failed", exc_info=True)
        return {"decisions": []}


def classify_batch(batch: list[dict]) -> list[dict]:
    user_prompt = _build_user_prompt(batch)
    result = call_deepseek(SYSTEM_PROMPT, user_prompt)
    decisions_by_index = {d.get("index"): d for d in result.get("decisions", [])}

    classified = []
    for i, item in enumerate(batch, start=1):
        decision = decisions_by_index.get(i)
        enriched = dict(item)
        if decision is None:
            logger.warning("No DeepSeek decision for item %d ('%s') - treating as not relevant", i, item["title"])
            enriched.update(relevant=False, category=item["category"], priority="low",
                             duplicate_of=None, reject_reason="no decision returned",
                             post_title=None, post_body=None)
        else:
            enriched.update(
                relevant=bool(decision.get("relevant", False)),
                category=decision.get("category", item["category"]),
                priority=decision.get("priority", "low"),
                duplicate_of=decision.get("duplicate_of"),
                reject_reason=decision.get("reject_reason"),
                post_title=decision.get("post_title"),
                post_body=decision.get("post_body"),
            )
        classified.append(enriched)
    return classified


def classify_items(items: list[dict]) -> list[dict]:
    classified = []
    batch_size = config.MAX_ITEMS_PER_CLASSIFY_BATCH
    for start in range(0, len(items), batch_size):
        batch = items[start:start + batch_size]
        classified.extend(classify_batch(batch))
    return classified
