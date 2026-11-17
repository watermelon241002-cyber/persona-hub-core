from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from persona_hub.db import Database
from persona_hub.llm_json import parse_json_object
from persona_hub.memory import MemoryGateway
from persona_hub.memory_reflection import CandidatePolicy, MemoryReflector


class FakeJsonModel:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def complete_json(self, *, system, payload, temperature, max_tokens, timeout_sec=None):
        self.calls.append({"system": system, "payload": payload, "temperature": temperature})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "reflect.sqlite3")
    database.initialize()
    return database


def turn(database: Database, user_text: str, assistant_text: str = "Okay."):
    conversation = database.create_conversation("t")
    user = database.append_message(conversation_id=conversation["id"], role="user", content=user_text)
    assistant = database.append_message(conversation_id=conversation["id"], role="assistant", content=assistant_text)
    return conversation["id"], user, assistant


def make_reflector(database: Database, model) -> MemoryReflector:
    return MemoryReflector(
        database=database,
        gateway=MemoryGateway(database),
        model=model,
        user_name="user",
        assistant_name="char",
    )


def reflect(reflector, conversation_id, user, assistant):
    return asyncio.run(
        reflector.reflect_turn(conversation_id=conversation_id, user_message=user, assistant_message=assistant)
    )


def test_do_not_surface_request_hides_the_turn_without_a_model(database):
    reflector = make_reflector(database, model=None)
    conversation_id, user, assistant = turn(database, "这个别存，我只是随口说说")
    result = reflect(reflector, conversation_id, user, assistant)
    assert result.mode == "do_not_surface"
    assert database.get_message(user["id"])["do_not_surface"] == 1
    assert database.get_message(assistant["id"])["do_not_surface"] == 1
    assert database.recent_visible_messages() == []


def test_english_do_not_surface_marker(database):
    reflector = make_reflector(database, model=FakeJsonModel())
    conversation_id, user, assistant = turn(database, "Please don't save this, ok?")
    assert reflect(reflector, conversation_id, user, assistant).mode == "do_not_surface"


def test_candidates_are_kept_queued_or_dropped_by_policy(database):
    model = FakeJsonModel(
        {
            "segment": {
                "title": "moving",
                "summary": "user said the new flat gets morning sun.",
                "quotes": ["**finally** a window that faces east"],
                "valence": 0.7,
                "arousal": 0.4,
            },
            "memory_candidates": [
                {"kind": "fact", "content": "user moved into a flat with an east-facing window", "score": 0.9},
                {"kind": "quote", "content": "user: finally a window that faces east", "score": 0.96, "importance": 6},
                {"kind": "emotion", "content": "char felt warm hearing about the flat", "score": 0.5},
                {"kind": "fact", "content": "A second fact in the same turn about the flat", "score": 0.95},
            ],
        }
    )
    reflector = make_reflector(database, model)
    conversation_id, user, assistant = turn(database, "We moved! The window faces east.")
    result = reflect(reflector, conversation_id, user, assistant)

    assert result.mode == "model"
    assert len(result.memory_ids) == 1
    memory = database.get_memory(result.memory_ids[0])
    assert memory["kind"] == "fact"
    assert memory["valence"] == 0.7  # inherited from the turn's segment
    assert len(result.candidate_ids) == 1
    assert database.get_memory_candidate(result.candidate_ids[0])["kind"] == "quote"
    assert result.skipped == 2  # low score + per-kind cap
    segment = database.list_memory_segments()[0]
    assert '"finally a window that faces east"' in segment["quotes_json"]
    assert model.calls[0]["temperature"] == 0.4


def test_duplicates_are_skipped(database):
    reflector = make_reflector(database, None)
    reflector.gateway.remember(content="user moved into a flat with an east-facing window", kind="fact")
    reflector.model = FakeJsonModel(
        {"memory_candidates": [{"kind": "fact", "content": "user moved into a flat with an east-facing window", "score": 0.9}]}
    )
    conversation_id, user, assistant = turn(database, "Did I tell you about the flat?")
    result = reflect(reflector, conversation_id, user, assistant)
    assert result.memory_ids == [] and result.skipped == 1


def test_recent_context_resolves_references_but_excludes_the_turn(database):
    model = FakeJsonModel({"memory_candidates": []})
    reflector = make_reflector(database, model)
    conversation = database.create_conversation("t")
    database.append_message(conversation_id=conversation["id"], role="user", content="Fixing the bike chain today")
    database.append_message(conversation_id=conversation["id"], role="assistant", content="Good luck with it")
    user = database.append_message(conversation_id=conversation["id"], role="user", content="Is it done yet? Yes!")
    assistant = database.append_message(conversation_id=conversation["id"], role="assistant", content="Nice.")
    reflect(reflector, conversation["id"], user, assistant)
    context = model.calls[0]["payload"]["recent_context"]
    assert [item["name"] for item in context] == ["user", "char"]
    assert context[0]["text"] == "Fixing the bike chain today"
    assert "the user is user and the assistant is char" in model.calls[0]["system"]


def test_segment_marked_hidden_hides_its_messages(database):
    model = FakeJsonModel({"segment": {"summary": "Something to forget.", "do_not_surface": True}})
    reflector = make_reflector(database, model)
    conversation_id, user, assistant = turn(database, "forget I said it")
    reflect(reflector, conversation_id, user, assistant)
    assert database.list_memory_segments() == []
    assert len(database.list_memory_segments(include_hidden=True)) == 1
    assert database.get_message(user["id"])["do_not_surface"] == 1


def test_model_failure_is_reported_with_its_type(database):
    reflector = make_reflector(database, FakeJsonModel(TimeoutError()))
    conversation_id, user, assistant = turn(database, "hello there, long day")
    result = reflect(reflector, conversation_id, user, assistant)
    assert result.error.startswith("TimeoutError")


def test_policy_drops_filler_and_names_only():
    policy = CandidatePolicy(filler_words=("user",), party_names=("user",))
    assert policy.should_drop("哈哈哈哈哈哈哈哈哈", 0.9)
    assert policy.should_drop("user user user user", 0.9)
    assert policy.should_drop("A perfectly fine memory", 0.4)
    assert not policy.should_drop("user prefers tea over coffee", 0.9)
    assert policy.should_keep(content="x", kind="preference", tags=[], score=0.85, importance=3)
    assert not policy.should_keep(content="x", kind="quote", tags=[], score=0.97, importance=5)
    assert policy.should_queue(kind="quote", tags=[], score=0.97)


def test_parse_json_object_tolerates_fences_and_rejects_truncation():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Here you go: {"a": 2} hope that helps') == {"a": 2}
    with pytest.raises(ValueError):
        parse_json_object('{"summary": "cut off')
    with pytest.raises(ValueError):
        parse_json_object("[1, 2]")
