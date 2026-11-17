"""Layered summaries: daily from messages, weekly from daily, monthly from weekly.

A summary here is written for a future self who needs to remember *how it
felt*, not for an auditor who needs to know *what happened*. So each row
carries three layers next to the prose:

- `quotes`: lines kept exactly as they were said;
- `scene`: a one or two sentence anchor (roughly when, doing what, in what state);
- `valence` / `arousal`: the emotional reading of the period.

Production lessons kept in this module:

- **The form decides the content.** A schema with only {summary, tags} gets
  dry narration no matter how good the model is. Give feelings a field.
- **Without a scheduler there are no summaries.** The private deployment
  silently stopped writing them for two months because only a manual endpoint
  existed. The scheduler below only fills *completed* periods, runs daily ->
  weekly -> monthly strictly in order (each layer reads the one below), and
  relies on the skip-existing check to stay idempotent.
- **Periods follow local time.** Messages are stored in UTC; a "day" cut at
  UTC midnight splits an evening in half for anyone far from Greenwich.
- **Record failures with their type.** A timeout's message is an empty
  string; an error log without the exception type is no log at all.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
import json
import logging
from typing import Any, Callable

from .db import Database
from .llm_json import JsonModel
from .memory_text import clean_quote, clean_tags, float_range, local_time_text, trim

logger = logging.getLogger("persona_hub.memory.summaries")

SUMMARY_TYPES = ("daily", "weekly", "monthly")
SOURCE_SUMMARY_TYPE = {"weekly": "daily", "monthly": "weekly"}
# How far back each scheduled pass looks. Existing periods are skipped, so a
# wide window only costs a few SELECTs, never a repeated model call.
LOOKBACK_DAYS = {"daily": 10, "weekly": 35, "monthly": 120}
SCHEDULE_WARMUP_SEC = 180.0
SCHEDULE_INTERVAL_SEC = 6 * 3600.0
LIMIT_PER_PERIOD = 2000
SENSITIVE_TAG = "sensitive"


def period_floor(value: datetime, summary_type: str) -> datetime:
    if summary_type == "weekly":
        value = value - timedelta(days=value.weekday())
    elif summary_type == "monthly":
        value = value.replace(day=1)
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


def period_step(value: datetime, summary_type: str) -> datetime:
    if summary_type == "weekly":
        return value + timedelta(days=7)
    if summary_type == "monthly":
        if value.month == 12:
            return value.replace(year=value.year + 1, month=1)
        return value.replace(month=value.month + 1)
    return value + timedelta(days=1)


def summary_periods(summary_type: str, start: datetime, end: datetime, tz: tzinfo) -> list[tuple[str, str]]:
    """Local-time periods covering [start, end), emitted as UTC ISO strings.

    UTC strings keep string comparison against stored `created_at` valid.
    """

    cursor = period_floor(start.astimezone(tz), summary_type)
    end_local = end.astimezone(tz)
    periods: list[tuple[str, str]] = []
    while cursor < end_local:
        following = period_step(cursor, summary_type)
        periods.append(
            (cursor.astimezone(timezone.utc).isoformat(), following.astimezone(timezone.utc).isoformat())
        )
        cursor = following
    return periods


def summary_prompt(*, user_name: str, assistant_name: str) -> tuple[str, list[str]]:
    system = (
        "You write layered summaries for a memory system. Return JSON only. "
        "You are writing the memory of a real relationship, not meeting minutes and not a "
        "case file. Keep facts, changes in the relationship, the emotional arc, recurring "
        "preferences, and the lines that carried weight. Most important of all: never turn a "
        "short, heavy line into an abstraction. Put it in quotes exactly as said. "
        f"In this relationship the user is {user_name} and the assistant is {assistant_name}. "
        f"Always write {user_name} and {assistant_name}; never 'the user', 'the assistant' or 'the AI'. "
        "Write in the language the source is in."
    )
    rules = [
        "quotes must be verbatim. No polishing, no rewording, no paraphrase.",
        "Which lines belong in quotes: the less they read like writing, the more they belong. "
        "Short lines, lines with a name or pet name, repetitions, swearing, lines said while "
        "clearly shaken.",
        "Do not nominalise what was said. Bad: 'A expressed a strong need for reassurance.' "
        "Good: 'A said: \"can you just stay on the phone for a bit\".'",
        "Avoid report verbs: conducted, expressed, confirmed, implemented, intervened, assessed, "
        "handled, communicated, provided (in Chinese: 进行了、表达了、确认了、实施、干预、评估、处理、"
        "沟通、给予). Write who said what and who did what.",
        "Bad: 'The two had a deep emotional exchange at a vulnerable moment.' Good: 'She was too "
        "tired to finish a sentence; in the end she only said goodnight twice.'",
        "This summary is for a future self: the goal is to bring back how it felt, not to list "
        "what happened.",
        "If something in this period should not be casually brought up again, still write it "
        f"down honestly, and add the tag '{SENSITIVE_TAG}'.",
        "Length: summary under 500 characters; at most 8 quotes, each under 60 characters.",
        "The output must be complete, valid JSON. Write fewer quotes rather than risk being cut "
        "off: a truncated reply loses the whole period.",
    ]
    return system, rules


SUMMARY_SCHEMA = {
    "summary": "summary that keeps the tone and warmth; not documentary narration",
    "quotes": ["important lines kept verbatim, especially short, heavy ones with names"],
    "scene": "anchor: roughly when, what they were doing, physical or mental state (one or two sentences)",
    "valence": "number -1..1, the period's emotional baseline (negative = sad or tense)",
    "arousal": "number 0..1, emotional intensity (0 = calm, 1 = intense)",
    "tags": ["topic or mood tags"],
}


@dataclass(slots=True)
class SummaryRunStats:
    summary_type: str
    available_periods: int = 0
    created: int = 0
    skipped_existing: int = 0
    skipped_empty: int = 0
    failed_periods: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary_type": self.summary_type,
            "available_periods": self.available_periods,
            "created": self.created,
            "skipped_existing": self.skipped_existing,
            "skipped_empty": self.skipped_empty,
            "failed_periods": self.failed_periods,
            "errors": list(self.errors),
        }


class Summarizer:
    def __init__(
        self,
        *,
        database: Database,
        model: JsonModel,
        tz: tzinfo = timezone.utc,
        user_name: str = "User",
        assistant_name: str = "Assistant",
        limit_per_period: int = LIMIT_PER_PERIOD,
    ):
        self.database = database
        self.model = model
        self.tz = tz
        self.user_name = user_name
        self.assistant_name = assistant_name
        self.limit_per_period = limit_per_period

    async def run(
        self, summary_type: str, start: datetime, end: datetime, *, force: bool = False
    ) -> SummaryRunStats:
        if summary_type not in SUMMARY_TYPES:
            raise ValueError(f"Unsupported summary_type: {summary_type}")
        stats = SummaryRunStats(summary_type=summary_type)
        periods = summary_periods(summary_type, start, end, self.tz)
        stats.available_periods = len(periods)
        for period_start, period_end in periods:
            try:
                outcome = await self.summarize_period(summary_type, period_start, period_end, force=force)
            except Exception as exc:
                stats.failed_periods += 1
                if len(stats.errors) < 20:
                    stats.errors.append(f"{period_start}: {type(exc).__name__}: {str(exc)[:240]}")
                continue
            if outcome == "created":
                stats.created += 1
            elif outcome == "skipped_existing":
                stats.skipped_existing += 1
            else:
                stats.skipped_empty += 1
            await asyncio.sleep(0)
        return stats

    async def summarize_period(
        self, summary_type: str, period_start: str, period_end: str, *, force: bool = False
    ) -> str:
        if not force and self.database.get_memory_summary_for_period(summary_type, period_start, period_end):
            return "skipped_existing"

        source_type = SOURCE_SUMMARY_TYPE.get(summary_type)
        if source_type:
            rows = self.database.list_memory_summaries(
                source_type, start_at=period_start, end_at=period_end, limit=self.limit_per_period
            )
            rows.sort(key=lambda row: row["period_start"])
            source = [
                {
                    "type": row["summary_type"],
                    "period": [
                        local_time_text(row["period_start"], self.tz, "%Y-%m-%d"),
                        local_time_text(row["period_end"], self.tz, "%Y-%m-%d"),
                    ],
                    "content": trim(row["content"], 1200),
                    "quotes": json.loads(row["quotes_json"] or "[]"),
                    "scene": row["scene"],
                    "tags": json.loads(row["tags_json"] or "[]"),
                }
                for row in rows[:80]
            ]
        else:
            rows = self.database.messages_between(period_start, period_end, limit=self.limit_per_period)
            names = {"user": self.user_name, "assistant": self.assistant_name}
            source = [
                {
                    "name": names.get(row["role"], row["role"]),
                    "time": local_time_text(row["created_at"], self.tz),
                    "text": trim(row["content"], 900),
                }
                for row in rows[:240]
            ]
        if not rows:
            return "skipped_empty"

        system, rules = summary_prompt(user_name=self.user_name, assistant_name=self.assistant_name)
        payload = {
            "summary_type": summary_type,
            "period_start": local_time_text(period_start, self.tz),
            "period_end": local_time_text(period_end, self.tz),
            "source": source,
            "schema": SUMMARY_SCHEMA,
            "rules": rules,
        }
        # Warmer than plain extraction: at 0.1 the model converges on stock
        # phrases. Summaries run in the background, so the timeout is generous
        # and the token budget fits a busy day.
        data = await self.model.complete_json(
            system=system, payload=payload, temperature=0.45, max_tokens=5000, timeout_sec=300.0
        )
        content = str(data.get("summary") or "").strip()
        if not content:
            raise ValueError("summary model returned an empty summary")
        self.database.add_memory_summary(
            summary_type=summary_type,
            period_start=period_start,
            period_end=period_end,
            content=content[:5000],
            quotes=[quote for quote in (clean_quote(item) for item in (data.get("quotes") or [])[:8]) if quote],
            scene=trim(str(data.get("scene") or "").strip(), 400),
            valence=float_range(data.get("valence"), 0.0, -1.0, 1.0),
            arousal=float_range(data.get("arousal"), 0.0, 0.0, 1.0),
            tags=clean_tags(data.get("tags")),
            source_ids=[str(row["id"]) for row in rows],
            replace=force,
        )
        return "created"


class SummaryScheduler:
    """Fill completed periods in the background, in dependency order."""

    def __init__(
        self,
        summarizer: Summarizer,
        *,
        warmup_sec: float = SCHEDULE_WARMUP_SEC,
        interval_sec: float = SCHEDULE_INTERVAL_SEC,
        clock: Callable[[], datetime] | None = None,
    ):
        self.summarizer = summarizer
        self.warmup_sec = warmup_sec
        self.interval_sec = interval_sec
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._task: asyncio.Task[None] | None = None

    async def run_once(self, now: datetime | None = None) -> dict[str, SummaryRunStats]:
        moment = now or self.clock()
        results: dict[str, SummaryRunStats] = {}
        # Strictly sequential: weekly reads daily, monthly reads weekly. Only
        # periods that have ended are summarised; today waits until it is whole.
        for summary_type in SUMMARY_TYPES:
            boundary = period_floor(moment.astimezone(self.summarizer.tz), summary_type)
            start = boundary - timedelta(days=LOOKBACK_DAYS[summary_type])
            results[summary_type] = await self.summarizer.run(summary_type, start, boundary)
        return results

    async def _loop(self) -> None:
        # Let startup, tool warmup and the first chat turn go first.
        await asyncio.sleep(self.warmup_sec)
        while True:
            try:
                for summary_type, stats in (await self.run_once()).items():
                    if stats.created or stats.failed_periods:
                        logger.info("summaries %s: %s", summary_type, stats.to_dict())
            except asyncio.CancelledError:
                raise
            except Exception:
                # Missing summaries are a memory problem; they must never take chat down.
                logger.exception("summary pass failed; retrying next interval")
            await asyncio.sleep(self.interval_sec)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
