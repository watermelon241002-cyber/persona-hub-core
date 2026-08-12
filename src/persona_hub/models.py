from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ConversationCreate(BaseModel):
    title: str = ""


class ConversationView(BaseModel):
    id: str
    title: str
    rolling_summary: str
    created_at: str
    updated_at: str


class MessageView(BaseModel):
    id: str
    conversation_id: str
    role: Literal["user", "assistant", "system", "tool"]
    content: str
    provider_id: str | None = None
    model_id: str | None = None
    request_id: str | None = None
    status: str
    created_at: str


class ChatRequest(BaseModel):
    conversation_id: str
    request_id: str = Field(min_length=8, max_length=200)
    content: str = Field(min_length=1, max_length=100_000)
    provider_id: str | None = None


class ChatResponse(BaseModel):
    request_id: str
    state: str
    replayed: bool
    user_message: MessageView
    assistant_message: MessageView
    recalled_memory_ids: list[str] = Field(default_factory=list)


class MemoryCreate(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)
    kind: str = "episodic"
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    occurred_at: str | None = None


class MemoryView(BaseModel):
    id: str
    kind: str
    content: str
    importance: float
    score: float | None = None
    occurred_at: str | None = None
    created_at: str


class RoomParticipantCreate(BaseModel):
    agent_id: str = Field(min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=80)
    provider_id: str = "echo"
    position: int = Field(ge=1, le=16)
    role_prompt: str = Field(default="Collaborate on the current request.", max_length=4000)


class RoomCreate(BaseModel):
    title: str = Field(default="Agent room", max_length=200)
    participants: list[RoomParticipantCreate] = Field(min_length=1, max_length=8)


class RoomTurnCreate(BaseModel):
    request_id: str = Field(min_length=8, max_length=200)
    content: str = Field(min_length=1, max_length=100_000)


class RoomMessageView(BaseModel):
    id: str
    room_id: str
    turn_id: str
    author_type: str
    author_id: str
    display_name: str
    content: str
    sequence_no: int
    created_at: str


class RoomTurnResponse(BaseModel):
    room_id: str
    turn_id: str
    request_id: str
    state: str
    replayed: bool
    messages: list[RoomMessageView]


class WorkerView(BaseModel):
    worker_id: str
    worker_type: str
    capabilities: list[str]
    version: str
    connected_at: str
    last_seen_at: str


class ErrorDetail(BaseModel):
    code: str
    message: str
    retryable: bool = False
    context: dict[str, Any] = Field(default_factory=dict)
