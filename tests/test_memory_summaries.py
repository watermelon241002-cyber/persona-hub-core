from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from persona_hub.context import ContextBuilder
from persona_hub.db import Database
from persona_hub.memory import MemoryGateway
from persona_hub.memory_summaries import Summarizer, SummaryScheduler, summary_periods

LOCAL_TZ = timezone(timedelta(hours=8))


class ScriptedSummaryModel:
    def __init__(self, fail_on: set[str] | None = None):
        self.calls = []
        self.fail_on = fail_on or set()

    async def complete_json(self, *, system, payload, temperature, max_tokens, timeout_sec=None):
        self.calls.append(payload)
        if payload["summary_type"] in self.fail_on:
            raise TimeoutError()
        return {
            "summary": f"{payload['summary_type']} summary of {len(payload['source'])} items",
            "quotes": ["**see you** tomorrow\n"],
            "scene": "late evening, on the sofa",
            "valence": 0.4,
            "arousal": 7,  # out of range on purpose
            "tags": ["evening"],
        }


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "summary.sqlite3")
    database.initialize()
    return database


def add_message(database: Database, conversation_id: str, when: datetime, text: str, role: str = "user"):
    return database.append_message(
        conversation_id=conversation_id,
        role=role,
        content=text,
        created_at=when.astimezone(timezone.utc).isoformat(),
    )


def test_periods_follow_local_midnight():
    start = datetime(2026, 1, 5, 0, 0, tzinfo=LOCAL_TZ)
    periods = summary_periods("daily", start, start + timedelta(days=2), LOCAL_TZ)
    assert periods == [
        ("2026-01-04T16:00:00+00:00", "2026-01-05T16:00:00+00:00"),
        ("2026-01-05T16:00:00+00:00", "2026-01-06T16:00:00+00:00"),
    ]
    weekly = summary_periods("weekly", datetime(2026, 1, 7, tzinfo=LOCAL_TZ), datetime(2026, 1, 8, tzinfo=LOCAL_TZ), LOCAL_TZ)
    assert weekly == [("2026-01-04T16:00:00+00:00", "2026-01-11T16:00:00+00:00")]  # Monday 5 Jan local
    monthly = summary_periods("monthly", datetime(2026, 12, 3, tzinfo=LOCAL_TZ), datetime(2026, 12, 4, tzinfo=LOCAL_TZ), LOCAL_TZ)
    assert monthly == [("2026-11-30T16:00:00+00:00", "2026-12-31T16:00:00+00:00")]


def test_evening_message_lands_in_the_local_day(database):
    conversation = database.create_conversation("c")
    # 23:30 local on 5 Jan is 15:30 UTC: still 5 Jan locally, and a UTC day cut would agree,
    # but 00:30 local on 6 Jan is 16:30 UTC on 5 Jan: a UTC cut would put it in the wrong day.
    add_message(database, conversation["id"], datetime(2026, 1, 5, 23, 30, tzinfo=LOCAL_TZ), "late")
    add_message(database, conversation["id"], datetime(2026, 1, 6, 0, 30, tzinfo=LOCAL_TZ), "after midnight")
    model = ScriptedSummaryModel()
    summarizer = Summarizer(database=database, model=model, tz=LOCAL_TZ, user_name="user", assistant_name="char")
    stats = asyncio.run(
        summarizer.run("daily", datetime(2026, 1, 5, tzinfo=LOCAL_TZ), datetime(2026, 1, 7, tzinfo=LOCAL_TZ))
    )
    assert stats.created == 2
    sources = [[item["text"] for item in call["source"]] for call in model.calls]
    assert sources == [["late"], ["after midnight"]]
    assert model.calls[0]["source"][0]["time"] == "2026-01-05 23:30"  # local, not UTC
    assert model.calls[0]["source"][0]["name"] == "user"


def test_summary_fields_are_cleaned_and_runs_are_idempotent(database):
    conversation = database.create_conversation("c")
    add_message(database, conversation["id"], datetime(2026, 1, 5, 20, 0, tzinfo=LOCAL_TZ), "hello")
    model = ScriptedSummaryModel()
    summarizer = Summarizer(database=database, model=model, tz=LOCAL_TZ)
    window = (datetime(2026, 1, 5, tzinfo=LOCAL_TZ), datetime(2026, 1, 6, tzinfo=LOCAL_TZ))
    asyncio.run(summarizer.run("daily", *window))
    row = database.list_memory_summaries("daily")[0]
    assert row["quotes_json"] == '["see you tomorrow"]'
    assert row["arousal"] == 1.0 and row["scene"] == "late evening, on the sofa"

    again = asyncio.run(summarizer.run("daily", *window))
    assert again.skipped_existing == 1 and len(model.calls) == 1
    forced = asyncio.run(summarizer.run("daily", *window, force=True))
    assert forced.created == 1 and len(database.list_memory_summaries("daily")) == 1


def test_hidden_messages_and_empty_days_are_not_summarised(database):
    conversation = database.create_conversation("c")
    hidden = add_message(database, conversation["id"], datetime(2026, 1, 5, 20, 0, tzinfo=LOCAL_TZ), "secret")
    database.mark_messages_do_not_surface([hidden["id"]])
    summarizer = Summarizer(database=database, model=ScriptedSummaryModel(), tz=LOCAL_TZ)
    stats = asyncio.run(
        summarizer.run("daily", datetime(2026, 1, 5, tzinfo=LOCAL_TZ), datetime(2026, 1, 6, tzinfo=LOCAL_TZ))
    )
    assert stats.skipped_empty == 1 and stats.created == 0


def test_failures_keep_the_exception_type(database):
    conversation = database.create_conversation("c")
    add_message(database, conversation["id"], datetime(2026, 1, 5, 20, 0, tzinfo=LOCAL_TZ), "hello")
    summarizer = Summarizer(database=database, model=ScriptedSummaryModel(fail_on={"daily"}), tz=LOCAL_TZ)
    stats = asyncio.run(
        summarizer.run("daily", datetime(2026, 1, 5, tzinfo=LOCAL_TZ), datetime(2026, 1, 6, tzinfo=LOCAL_TZ))
    )
    assert stats.failed_periods == 1
    assert "TimeoutError" in stats.errors[0]


def test_scheduler_builds_layers_in_order_and_only_completed_periods(database):
    conversation = database.create_conversation("c")
    for day in range(5, 13):  # Mon 5 Jan .. Mon 12 Jan
        add_message(database, conversation["id"], datetime(2026, 1, day, 21, 0, tzinfo=LOCAL_TZ), f"day {day}")
    model = ScriptedSummaryModel()
    scheduler = SummaryScheduler(Summarizer(database=database, model=model, tz=LOCAL_TZ))
    results = asyncio.run(scheduler.run_once(datetime(2026, 1, 12, 22, 0, tzinfo=LOCAL_TZ)))

    daily = database.list_memory_summaries("daily", limit=100)
    assert len(daily) == 7  # 5..11 Jan; today (12 Jan) is not finished yet
    weekly = database.list_memory_summaries("weekly")
    assert len(weekly) == 1
    weekly_call = next(call for call in model.calls if call["summary_type"] == "weekly")
    assert len(weekly_call["source"]) == 7  # built from the daily layer, not raw messages
    assert results["monthly"].created == 0  # January has not ended


def test_context_shows_recent_summaries_but_not_sensitive_ones(database, tmp_path):
    for day, tags in ((5, '["evening"]'), (6, '["sensitive"]'), (7, "[]")):
        start = datetime(2026, 1, day, tzinfo=LOCAL_TZ).astimezone(timezone.utc).isoformat()
        end = datetime(2026, 1, day + 1, tzinfo=LOCAL_TZ).astimezone(timezone.utc).isoformat()
        database.add_memory_summary(
            summary_type="daily",
            period_start=start,
            period_end=end,
            content=f"summary for day {day}",
            quotes=["night night"] if day == 7 else [],
            scene="",
            valence=0.1,
            arousal=0.2,
            tags=[],
            source_ids=[],
        )
        database._connection.execute(
            "UPDATE memory_summaries SET tags_json = ? WHERE period_start = ?", (tags, start)
        )
    persona = tmp_path / "persona.md"
    persona.write_text("Persona.", encoding="utf-8")
    builder = ContextBuilder(
        database=database, memory_gateway=MemoryGateway(database), persona_file=persona, tz=LOCAL_TZ
    )
    conversation = database.create_conversation("c")
    prompt = builder.build(conversation["id"], "hi").system_prompt
    assert "summary for day 6" not in prompt
    assert "summary for day 5" in prompt and "summary for day 7" in prompt
    assert prompt.index("day 5") < prompt.index("day 7")
    assert "[daily from 2026-01-07]" in prompt and "night night" in prompt
