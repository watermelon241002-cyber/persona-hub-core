from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any, Iterator
from uuid import uuid4


class RequestPayloadConflictError(ValueError):
    """The request_id was already used with a different payload."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def payload_fingerprint(*parts: str) -> str:
    joined = "\x1f".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    rolling_summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
    content TEXT NOT NULL,
    provider_id TEXT,
    model_id TEXT,
    request_id TEXT,
    status TEXT NOT NULL DEFAULT 'completed',
    created_at TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
);

CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    state TEXT NOT NULL,
    user_message_id TEXT NOT NULL,
    assistant_message_id TEXT,
    provider_id TEXT NOT NULL,
    recalled_memory_ids_json TEXT NOT NULL DEFAULT '[]',
    payload_hash TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id),
    FOREIGN KEY (user_message_id) REFERENCES messages(id),
    FOREIGN KEY (assistant_message_id) REFERENCES messages(id)
);

CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0.5,
    embedding_json TEXT NOT NULL,
    occurred_at TEXT,
    pinned INTEGER NOT NULL DEFAULT 0,
    valence REAL,
    arousal REAL,
    quotes_json TEXT NOT NULL DEFAULT '[]',
    scene TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_rooms (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'sequential',
    state TEXT NOT NULL DEFAULT 'waiting_human',
    current_turn_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_room_participants (
    room_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    role_prompt TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (room_id, agent_id),
    UNIQUE (room_id, position),
    FOREIGN KEY (room_id) REFERENCES agent_rooms(id)
);

CREATE TABLE IF NOT EXISTS agent_room_turns (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    turn_no INTEGER NOT NULL,
    state TEXT NOT NULL,
    payload_hash TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (room_id, turn_no),
    FOREIGN KEY (room_id) REFERENCES agent_rooms(id)
);

CREATE TABLE IF NOT EXISTS agent_room_messages (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    author_type TEXT NOT NULL,
    author_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    content TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (room_id, sequence_no),
    FOREIGN KEY (room_id) REFERENCES agent_rooms(id),
    FOREIGN KEY (turn_id) REFERENCES agent_room_turns(id)
);

CREATE TABLE IF NOT EXISTS agent_room_deliveries (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    attempt INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL,
    claimed_at TEXT,
    lease_until TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (turn_id, agent_id, attempt),
    FOREIGN KEY (room_id) REFERENCES agent_rooms(id),
    FOREIGN KEY (turn_id) REFERENCES agent_room_turns(id)
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_created
ON messages(conversation_id, created_at);

CREATE INDEX IF NOT EXISTS idx_memories_created
ON memories(created_at);

CREATE INDEX IF NOT EXISTS idx_room_messages_turn_sequence
ON agent_room_messages(turn_id, sequence_no);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path) if path != ":memory:" else Path(":memory:")
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()

    def initialize(self) -> None:
        with self._lock:
            self._connection.executescript(SCHEMA)
            # Databases created before payload verification lack this column.
            for table in ("requests", "agent_room_turns"):
                try:
                    self._connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN payload_hash TEXT"
                    )
                except sqlite3.OperationalError:
                    pass
            # Databases created before affect-aware recall lack these columns.
            # `pinned` carries memories that must surface regardless of topic;
            # valence/arousal give recall an emotional axis that an embedding
            # cannot provide on its own.
            for column, definition in (
                ("pinned", "INTEGER NOT NULL DEFAULT 0"),
                ("valence", "REAL"),
                ("arousal", "REAL"),
                ("quotes_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("scene", "TEXT NOT NULL DEFAULT ''"),
            ):
                try:
                    self._connection.execute(
                        f"ALTER TABLE memories ADD COLUMN {column} {definition}"
                    )
                except sqlite3.OperationalError:
                    pass

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def fetchone(self, query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(query, params).fetchone()
        return dict(row) if row is not None else None

    def fetchall(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def create_conversation(self, title: str = "") -> dict[str, Any]:
        now = utc_now()
        conversation_id = str(uuid4())
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO conversations VALUES (?, ?, '', ?, ?)",
                (conversation_id, title, now, now),
            )
        return self.get_conversation(conversation_id)

    def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        row = self.fetchone(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        )
        if row is None:
            raise KeyError(f"Conversation not found: {conversation_id}")
        return row

    def list_messages(self, conversation_id: str, limit: int = 100) -> list[dict[str, Any]]:
        return self.fetchall(
            """
            SELECT * FROM (
                SELECT messages.*, rowid AS internal_rowid FROM messages
                WHERE conversation_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
            ) ORDER BY created_at ASC, internal_rowid ASC
            """,
            (conversation_id, limit),
        )

    def begin_chat_request(
        self,
        *,
        request_id: str,
        conversation_id: str,
        provider_id: str,
        content: str,
    ) -> tuple[dict[str, Any], bool]:
        now = utc_now()
        fingerprint = payload_fingerprint(conversation_id, provider_id, content)
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if existing is not None:
                stored = existing["payload_hash"]
                if stored and stored != fingerprint:
                    raise RequestPayloadConflictError(
                        "request_id was already used with a different payload;"
                        " use a new request_id"
                    )
                return dict(existing), True

            conversation = connection.execute(
                "SELECT id FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if conversation is None:
                raise KeyError(f"Conversation not found: {conversation_id}")

            user_message_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO messages
                (id, conversation_id, role, content, provider_id, model_id,
                 request_id, status, created_at)
                VALUES (?, ?, 'user', ?, NULL, NULL, ?, 'completed', ?)
                """,
                (user_message_id, conversation_id, content, request_id, now),
            )
            connection.execute(
                """
                INSERT INTO requests
                (request_id, conversation_id, state, user_message_id,
                 assistant_message_id, provider_id, recalled_memory_ids_json,
                 payload_hash, error_json, created_at, updated_at)
                VALUES (?, ?, 'pending', ?, NULL, ?, '[]', ?, NULL, ?, ?)
                """,
                (
                    request_id,
                    conversation_id,
                    user_message_id,
                    provider_id,
                    fingerprint,
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )
            row = connection.execute(
                "SELECT * FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        return dict(row), False

    def complete_chat_request(
        self,
        *,
        request_id: str,
        content: str,
        provider_id: str,
        model_id: str,
        recalled_memory_ids: list[str],
    ) -> dict[str, Any]:
        now = utc_now()
        with self.transaction() as connection:
            request = connection.execute(
                "SELECT * FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if request is None:
                raise KeyError(f"Request not found: {request_id}")
            if request["state"] == "completed":
                return dict(request)

            assistant_message_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO messages
                (id, conversation_id, role, content, provider_id, model_id,
                 request_id, status, created_at)
                VALUES (?, ?, 'assistant', ?, ?, ?, ?, 'completed', ?)
                """,
                (
                    assistant_message_id,
                    request["conversation_id"],
                    content,
                    provider_id,
                    model_id,
                    request_id,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE requests
                SET state = 'completed', assistant_message_id = ?,
                    provider_id = ?, recalled_memory_ids_json = ?, updated_at = ?
                WHERE request_id = ?
                """,
                (
                    assistant_message_id,
                    provider_id,
                    json.dumps(recalled_memory_ids),
                    now,
                    request_id,
                ),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, request["conversation_id"]),
            )
            row = connection.execute(
                "SELECT * FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        return dict(row)

    def fail_chat_request(self, request_id: str, error: dict[str, Any]) -> None:
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE requests
                SET state = 'failed', error_json = ?, updated_at = ?
                WHERE request_id = ?
                """,
                (json.dumps(error), now, request_id),
            )

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        return self.fetchone("SELECT * FROM requests WHERE request_id = ?", (request_id,))

    def get_message(self, message_id: str) -> dict[str, Any]:
        row = self.fetchone("SELECT * FROM messages WHERE id = ?", (message_id,))
        if row is None:
            raise KeyError(f"Message not found: {message_id}")
        return row

    def add_memory(
        self,
        *,
        kind: str,
        content: str,
        importance: float,
        embedding: list[float],
        occurred_at: str | None,
        pinned: bool = False,
        valence: float | None = None,
        arousal: float | None = None,
        quotes: list[str] | None = None,
        scene: str = "",
    ) -> dict[str, Any]:
        memory_id = str(uuid4())
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO memories
                (id, kind, content, importance, embedding_json, occurred_at,
                 pinned, valence, arousal, quotes_json, scene,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    kind,
                    content,
                    importance,
                    json.dumps(embedding),
                    occurred_at,
                    1 if pinned else 0,
                    valence,
                    arousal,
                    json.dumps(quotes or []),
                    scene,
                    now,
                    now,
                ),
            )
        return self.get_memory(memory_id)

    def get_memory(self, memory_id: str) -> dict[str, Any]:
        row = self.fetchone("SELECT * FROM memories WHERE id = ?", (memory_id,))
        if row is None:
            raise KeyError(f"Memory not found: {memory_id}")
        return row

    def list_memories(self) -> list[dict[str, Any]]:
        return self.fetchall("SELECT * FROM memories ORDER BY created_at DESC")

    def create_room(self, title: str, participants: list[dict[str, Any]]) -> dict[str, Any]:
        room_id = str(uuid4())
        now = utc_now()
        positions = [int(item["position"]) for item in participants]
        if len(positions) != len(set(positions)):
            raise ValueError("Room participant positions must be unique")
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO agent_rooms
                (id, title, mode, state, current_turn_id, created_at, updated_at)
                VALUES (?, ?, 'sequential', 'waiting_human', NULL, ?, ?)
                """,
                (room_id, title, now, now),
            )
            for item in participants:
                connection.execute(
                    """
                    INSERT INTO agent_room_participants
                    (room_id, agent_id, display_name, provider_id, position,
                     role_prompt, enabled)
                    VALUES (?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        room_id,
                        item["agent_id"],
                        item["display_name"],
                        item["provider_id"],
                        item["position"],
                        item["role_prompt"],
                    ),
                )
        return self.get_room(room_id)

    def get_room(self, room_id: str) -> dict[str, Any]:
        row = self.fetchone("SELECT * FROM agent_rooms WHERE id = ?", (room_id,))
        if row is None:
            raise KeyError(f"Room not found: {room_id}")
        return row

    def get_room_participants(self, room_id: str) -> list[dict[str, Any]]:
        return self.fetchall(
            """
            SELECT * FROM agent_room_participants
            WHERE room_id = ? AND enabled = 1
            ORDER BY position ASC
            """,
            (room_id,),
        )

    def get_room_turn_by_request(self, request_id: str) -> dict[str, Any] | None:
        return self.fetchone(
            "SELECT * FROM agent_room_turns WHERE request_id = ?", (request_id,)
        )

    def begin_room_turn(
        self, *, room_id: str, request_id: str, content: str
    ) -> tuple[dict[str, Any], bool]:
        now = utc_now()
        fingerprint = payload_fingerprint(room_id, content)
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM agent_room_turns WHERE request_id = ?", (request_id,)
            ).fetchone()
            if existing is not None:
                stored = existing["payload_hash"]
                if stored and stored != fingerprint:
                    raise RequestPayloadConflictError(
                        "request_id was already used with a different payload;"
                        " use a new request_id"
                    )
                return dict(existing), True

            room = connection.execute(
                "SELECT * FROM agent_rooms WHERE id = ?", (room_id,)
            ).fetchone()
            if room is None:
                raise KeyError(f"Room not found: {room_id}")
            if room["state"] != "waiting_human":
                raise RuntimeError(f"Room is not waiting for a human: {room['state']}")

            turn_no = connection.execute(
                "SELECT COALESCE(MAX(turn_no), 0) + 1 FROM agent_room_turns WHERE room_id = ?",
                (room_id,),
            ).fetchone()[0]
            sequence_no = connection.execute(
                "SELECT COALESCE(MAX(sequence_no), 0) + 1 FROM agent_room_messages WHERE room_id = ?",
                (room_id,),
            ).fetchone()[0]
            turn_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO agent_room_turns
                (id, room_id, request_id, turn_no, state, payload_hash,
                 started_at, completed_at)
                VALUES (?, ?, ?, ?, 'running', ?, ?, NULL)
                """,
                (turn_id, room_id, request_id, turn_no, fingerprint, now),
            )
            connection.execute(
                """
                INSERT INTO agent_room_messages
                (id, room_id, turn_id, author_type, author_id, display_name,
                 content, sequence_no, created_at)
                VALUES (?, ?, ?, 'human', 'human', 'Human', ?, ?, ?)
                """,
                (str(uuid4()), room_id, turn_id, content, sequence_no, now),
            )
            connection.execute(
                """
                UPDATE agent_rooms
                SET state = 'waiting_agent', current_turn_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (turn_id, now, room_id),
            )
            row = connection.execute(
                "SELECT * FROM agent_room_turns WHERE id = ?", (turn_id,)
            ).fetchone()
        return dict(row), False

    def create_delivery(
        self, *, room_id: str, turn_id: str, agent_id: str, request_id: str
    ) -> dict[str, Any]:
        now = utc_now()
        delivery_id = str(uuid4())
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM agent_room_deliveries WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if existing is not None:
                return dict(existing)
            connection.execute(
                """
                INSERT INTO agent_room_deliveries
                (id, room_id, turn_id, agent_id, request_id, attempt, state,
                 claimed_at, lease_until, error_json, created_at, completed_at)
                VALUES (?, ?, ?, ?, ?, 1, 'queued', NULL, NULL, NULL, ?, NULL)
                """,
                (delivery_id, room_id, turn_id, agent_id, request_id, now),
            )
            row = connection.execute(
                "SELECT * FROM agent_room_deliveries WHERE id = ?", (delivery_id,)
            ).fetchone()
        return dict(row)

    def set_delivery_state(
        self, delivery_id: str, state: str, error: dict[str, Any] | None = None
    ) -> None:
        now = utc_now()
        completed_at = now if state in {"completed", "failed", "timed_out", "skipped", "cancelled"} else None
        claimed_at = now if state == "claimed" else None
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE agent_room_deliveries
                SET state = ?,
                    claimed_at = COALESCE(?, claimed_at),
                    completed_at = COALESCE(?, completed_at),
                    error_json = ?
                WHERE id = ?
                """,
                (state, claimed_at, completed_at, json.dumps(error) if error else None, delivery_id),
            )

    def append_room_message(
        self,
        *,
        room_id: str,
        turn_id: str,
        author_type: str,
        author_id: str,
        display_name: str,
        content: str,
    ) -> dict[str, Any]:
        now = utc_now()
        message_id = str(uuid4())
        with self.transaction() as connection:
            sequence_no = connection.execute(
                "SELECT COALESCE(MAX(sequence_no), 0) + 1 FROM agent_room_messages WHERE room_id = ?",
                (room_id,),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO agent_room_messages
                (id, room_id, turn_id, author_type, author_id, display_name,
                 content, sequence_no, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    room_id,
                    turn_id,
                    author_type,
                    author_id,
                    display_name,
                    content,
                    sequence_no,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM agent_room_messages WHERE id = ?", (message_id,)
            ).fetchone()
        return dict(row)

    def complete_room_turn(self, room_id: str, turn_id: str) -> None:
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE agent_room_turns
                SET state = 'completed', completed_at = ? WHERE id = ?
                """,
                (now, turn_id),
            )
            connection.execute(
                """
                UPDATE agent_rooms
                SET state = 'waiting_human', current_turn_id = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, room_id),
            )

    def fail_room_turn(self, room_id: str, turn_id: str) -> None:
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                "UPDATE agent_room_turns SET state = 'failed', completed_at = ? WHERE id = ?",
                (now, turn_id),
            )
            connection.execute(
                """
                UPDATE agent_rooms
                SET state = 'waiting_human', current_turn_id = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, room_id),
            )

    def list_room_messages(self, turn_id: str) -> list[dict[str, Any]]:
        return self.fetchall(
            """
            SELECT * FROM agent_room_messages
            WHERE turn_id = ? ORDER BY sequence_no ASC
            """,
            (turn_id,),
        )
