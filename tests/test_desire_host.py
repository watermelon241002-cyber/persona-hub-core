from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from persona_hub.db import Database
from persona_hub.desire import DesireContextUnavailable, DesireHost, Gates, OutputGuard, build_desire_messages
from persona_hub.desire import kernel

NOON_UTC = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


class Recorder:
    def __init__(self) -> None:
        self.records = []

    async def __call__(self, record) -> None:
        self.records.append(record)


def make_host(tmp_path: Path, *, speaker=None, actor=None, sink=None, clock=NOON_UTC, **kwargs) -> DesireHost:
    return DesireHost(
        state_path=tmp_path / "desire.json",
        gates=Gates(driven=True),
        clock=lambda: clock,
        speaker=speaker,
        actor=actor,
        sink=sink if sink is not None else Recorder(),
        **kwargs,
    )


def want(host: DesireHost, drive: str, level: float = 0.6) -> None:
    host.state.drive.update({key: 0.05 for key in host.state.drive})
    host.state.drive[drive] = level


def test_speaks_when_driven_and_records_it(tmp_path):
    async def speaker(intent):
        return "Thinking of you."

    host = make_host(tmp_path, speaker=speaker)
    want(host, "attachment")
    result = asyncio.run(host.beat())
    assert result.spoke and not result.retry_soon
    assert host.sink.records[0].kind == "murmur"
    assert host.state.spoke_today == 1
    assert host.state.refractory["attachment"] == kernel.REFRACTORY_TICKS
    assert json.loads((tmp_path / "desire.json").read_text(encoding="utf-8"))["last_spoke_text"] == "Thinking of you."


def test_quiet_hours_never_speak(tmp_path):
    async def speaker(intent):
        raise AssertionError("must not be called during quiet hours")

    host = make_host(tmp_path, speaker=speaker, clock=datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc))
    want(host, "attachment")
    result = asyncio.run(host.beat())
    assert result.quiet and not result.spoke
    assert result.interval_s >= kernel.HEARTBEAT_QUIET_FLOOR_S


def test_quiet_hours_use_local_time_and_may_wrap_midnight(tmp_path):
    local_tz = timezone(timedelta(hours=8))
    host = make_host(tmp_path, tz=local_tz, clock=datetime(2026, 1, 1, 19, 0, tzinfo=timezone.utc))
    assert host.is_quiet()  # 03:00 local
    wrapping = make_host(tmp_path, quiet_hours=(23, 7), clock=datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc))
    assert wrapping.is_quiet()


def test_gate_off_never_acts(tmp_path):
    async def speaker(intent):
        raise AssertionError("gate is off")

    host = make_host(tmp_path, speaker=speaker)
    host.gates = Gates()
    want(host, "attachment")
    result = asyncio.run(host.beat())
    assert not result.spoke and not result.acted


def test_speech_generation_failure_retries_soon_without_a_canned_line(tmp_path):
    async def speaker(intent):
        raise DesireContextUnavailable("no history")

    host = make_host(tmp_path, speaker=speaker)
    want(host, "attachment")
    result = asyncio.run(host.beat())
    assert not result.spoke and result.retry_soon
    assert result.interval_s <= 300
    assert host.sink.records == []


@pytest.mark.parametrize(
    "reply",
    [
        "I can't help with that request.",
        "thinking\nThe user wants...",
        "API Error: 401 insufficient balance",
        "Tool call: search_feeds\n{}",
    ],
)
def test_guarded_speech_is_dropped_without_retry(tmp_path, reply):
    async def speaker(intent):
        return reply

    host = make_host(tmp_path, speaker=speaker)
    want(host, "attachment")
    result = asyncio.run(host.beat())
    assert not result.spoke and not result.retry_soon
    assert host.sink.records == []


def test_strict_guard_for_non_english_personas():
    guard = OutputGuard(english_first_person_is_refusal=True)
    assert guard.refusal("I'm going to stay with the persona")
    assert not guard.refusal("想你了。")
    assert not OutputGuard().refusal("I missed you today.")


def test_outward_action_success_records_and_satisfies(tmp_path):
    calls = []

    async def actor(action, prompt):
        calls.append((action, prompt))
        return "**Wrote** a few lines about the rain.\nSecond line."

    host = make_host(tmp_path, actor=actor)
    want(host, "reflection")
    host.state.tick_count = -1  # next tick is 0 -> write_diary
    result = asyncio.run(host.beat())
    assert result.acted
    assert calls[0][0] == "write_diary"
    record = host.sink.records[0]
    assert record.kind == "diary" and "**" not in record.text
    assert host.state.acted_today == 1
    assert any(thought.text == "Wrote a few lines about the rain." for thought in host.state.thoughts)


def test_failed_action_takes_its_drive_out_for_a_while(tmp_path):
    async def actor(action, prompt):
        return ""

    host = make_host(tmp_path, actor=actor, failure_refractory_ticks=3)
    want(host, "reflection")
    result = asyncio.run(host.beat())
    assert not result.acted and not result.retry_soon
    assert host.state.refractory["reflection"] == 3
    assert host.state.acted_today == 0


def test_hint_is_filled_into_the_prompt(tmp_path):
    prompts = []

    async def actor(action, prompt):
        prompts.append(prompt)
        return "Read about tide pools."

    host = make_host(tmp_path, actor=actor)
    want(host, "curiosity")
    host.state.tick_count = -1  # explore_world
    kernel.add_thought(host.state, "tide pools", "curiosity", strength=0.5)
    asyncio.run(host.beat())
    assert "tide pools" in prompts[0] and "{hint}" not in prompts[0]


def test_on_user_message_never_raises(tmp_path):
    host = make_host(tmp_path)
    host.state_path = tmp_path / "missing-dir" / "\0invalid"
    host.on_user_message()  # save fails internally; chat must not notice


def test_state_survives_restart(tmp_path):
    host = make_host(tmp_path)
    host.on_user_message()
    attachment = host.state.drive["attachment"]
    again = make_host(tmp_path)
    assert again.state.drive["attachment"] == round(attachment, 4)


def seeded_database(tmp_path: Path) -> tuple[Database, str]:
    database = Database(tmp_path / "ctx.sqlite3")
    database.initialize()
    conversation = database.create_conversation("ctx")
    for index in range(30):
        database.append_message(
            conversation_id=conversation["id"],
            role="user" if index % 2 == 0 else "assistant",
            content=f"message {index} " + "x" * 200,
            created_at=f"2026-01-01T10:{index:02d}:00+00:00",
        )
    return database, conversation["id"]


def test_desire_context_trims_oldest_first_and_says_so(tmp_path):
    database, _ = seeded_database(tmp_path)
    full = build_desire_messages(database, prompt="say hi", now=NOON_UTC)
    trimmed = build_desire_messages(database, prompt="say hi", now=NOON_UTC, max_bytes=3000)
    assert len(json.dumps(trimmed, ensure_ascii=False).encode("utf-8")) <= 3000
    content = trimmed[0]["content"]
    assert "message 29" in content and "message 0 " not in content
    assert "omitted_older_messages" in content
    assert "omitted_older_messages" not in full[0]["content"]
    assert content.rstrip().endswith("say hi")


def test_desire_context_skips_hidden_messages_and_redacts_secrets(tmp_path):
    database, conversation_id = seeded_database(tmp_path)
    hidden = database.append_message(conversation_id=conversation_id, role="user", content="keep this private")
    database.append_message(conversation_id=conversation_id, role="user", content="the api_key=abc123secret")
    database.mark_messages_do_not_surface([hidden["id"]])
    content = build_desire_messages(database, prompt="task", now=NOON_UTC)[0]["content"]
    assert "keep this private" not in content
    assert "abc123secret" not in content


def test_desire_context_failure_does_not_leak(tmp_path):
    class Broken:
        def recent_visible_messages(self, limit):
            raise RuntimeError("private text here")

    with pytest.raises(DesireContextUnavailable) as excinfo:
        build_desire_messages(Broken(), prompt="task")
    assert "private" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
