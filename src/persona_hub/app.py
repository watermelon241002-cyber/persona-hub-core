from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import secrets
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import JSONResponse

from .config import Settings
from .context import ContextBuilder
from .db import Database, RequestPayloadConflictError
from .desire import ActivityRecord, DesireHost, Gates, Intent, build_desire_messages
from .llm_json import JsonModel, OpenAICompatibleJsonModel
from .memory import MemoryGateway
from .memory_reflection import MemoryReflector
from .memory_summaries import LOOKBACK_DAYS, Summarizer, SummaryScheduler, period_floor
from .memory_text import parse_timezone
from .models import (
    ChatRequest,
    ChatResponse,
    ConversationCreate,
    ConversationView,
    MemoryCreate,
    MemoryView,
    MessageView,
    RoomCreate,
    RoomTurnCreate,
    RoomTurnResponse,
    SummaryRunRequest,
    WorkerView,
)
from .providers import (
    EchoProvider,
    GenerationRequest,
    OpenAICompatibleProvider,
    ProviderRegistry,
)
from .services import (
    ChatService,
    PreviousRequestFailedError,
    RequestInProgressError,
    RoomService,
)
from .workers import WorkerRegistry


def _memory_row_to_view(row: dict[str, Any]) -> dict[str, Any]:
    """Shape a stored memory row for the API.

    The embedding never leaves the process, and `quotes` is exposed as a list
    rather than the raw JSON column.
    """

    data = dict(row)
    data.pop("embedding_json", None)
    quotes_raw = data.pop("quotes_json", "[]")
    try:
        data["quotes"] = json.loads(quotes_raw) if isinstance(quotes_raw, str) else list(quotes_raw or [])
    except json.JSONDecodeError:
        data["quotes"] = []
    data["pinned"] = bool(data.get("pinned", 0))
    return data


def _decode_json_columns(row: dict[str, Any]) -> dict[str, Any]:
    """Expose `foo_json` columns as decoded `foo` fields."""

    data = dict(row)
    for key in [key for key in data if key.endswith("_json")]:
        raw = data.pop(key)
        try:
            data[key[: -len("_json")]] = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            data[key[: -len("_json")]] = None
    if "do_not_surface" in data:
        data["do_not_surface"] = bool(data["do_not_surface"])
    return data


DESIRE_SPEAK_PROMPT = (
    "(You woke up on your own: {reason}. If something feels right to say to them now, say it "
    "in one or two short sentences, first person, in your own voice, no greeting formula. "
    "Respect anything they said recently about being busy or not wanting messages. If nothing "
    "feels right, reply with nothing at all.)"
)
DESIRE_CONTEXT_MAX_BYTES = 120_000


def create_app(
    settings: Settings | None = None,
    *,
    database: Database | None = None,
    providers: ProviderRegistry | None = None,
    memory_model: JsonModel | None = None,
    desire_gates: Gates | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate_for_production()
    tz = parse_timezone(settings.timezone)
    quiet_hours = settings.quiet_hours()
    database = database or Database(settings.database_path)
    database.initialize()

    providers = providers or ProviderRegistry()
    if "echo" not in providers.ids():
        providers.register(EchoProvider())
    if (
        settings.openai_base_url
        and settings.openai_api_key
        and settings.openai_model
        and "openai-compatible" not in providers.ids()
    ):
        providers.register(
            OpenAICompatibleProvider(
                base_url=settings.openai_base_url,
                api_key=settings.openai_api_key,
                model=settings.openai_model,
            )
        )

    memory_gateway = MemoryGateway(database)
    context_builder = ContextBuilder(
        database=database,
        memory_gateway=memory_gateway,
        persona_file=settings.persona_file,
        tz=tz,
    )
    try:
        persona_text = Path(settings.persona_file).read_text(encoding="utf-8")
    except FileNotFoundError:
        persona_text = "You are a careful, continuous collaborator."

    if (
        memory_model is None
        and settings.memory_model
        and settings.openai_base_url
        and settings.openai_api_key
    ):
        memory_model = OpenAICompatibleJsonModel(
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
            model=settings.memory_model,
        )
    names = {
        "user_name": settings.user_display_name,
        "assistant_name": settings.assistant_display_name,
    }
    # The reflector always runs: honouring a request not to keep something
    # needs no model.
    reflector = MemoryReflector(
        database=database,
        gateway=memory_gateway,
        model=memory_model if settings.memory_reflection else None,
        **names,
    )
    summarizer = (
        Summarizer(database=database, model=memory_model, tz=tz, **names)
        if memory_model is not None
        else None
    )
    summary_scheduler = (
        SummaryScheduler(summarizer)
        if summarizer is not None and settings.memory_summaries
        else None
    )

    async def speak(intent: Intent) -> str:
        messages = build_desire_messages(
            database,
            prompt=DESIRE_SPEAK_PROMPT.format(reason=intent.reason),
            tz=tz,
            max_bytes=DESIRE_CONTEXT_MAX_BYTES - len(persona_text.encode("utf-8")),
        )
        result = await providers.get(settings.default_provider).generate(
            GenerationRequest(
                system_prompt=persona_text,
                messages=messages,
                metadata={"purpose": "desire_speak", "drive": intent.drive_key},
            )
        )
        return result.text

    async def record_activity(record: ActivityRecord) -> None:
        database.add_desire_activity(
            kind=record.kind, action=record.action, drive=record.drive, content=record.text
        )

    # Outward actions need a worker that can use tools (roadmap 0.2), so no
    # actor is wired here; the kernel still chooses them and the host
    # dispatches them as soon as one is supplied.
    desire_host = DesireHost(
        state_path=settings.desire_state_path,
        gates=desire_gates or Gates.from_env(),
        tz=tz,
        quiet_hours=quiet_hours,
        speaker=speak,
        sink=record_activity,
    )

    async def reflect(conversation_id: str, user_message: dict, assistant_message: dict) -> None:
        await reflector.reflect_turn(
            conversation_id=conversation_id,
            user_message=user_message,
            assistant_message=assistant_message,
        )

    chat_service = ChatService(
        database=database,
        context_builder=context_builder,
        providers=providers,
        default_provider=settings.default_provider,
        on_user_message=desire_host.on_user_message,
        after_turn=reflect,
    )
    room_service = RoomService(
        database=database, providers=providers, persona_text=persona_text
    )
    worker_registry = WorkerRegistry(settings.worker_token)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if settings.desire_enabled:
            desire_host.start()
        if summary_scheduler is not None:
            summary_scheduler.start()
        try:
            yield
        finally:
            await desire_host.stop()
            if summary_scheduler is not None:
                await summary_scheduler.stop()
            await chat_service.drain()
            database.close()

    app = FastAPI(
        title="Persona Hub Core",
        version="0.1.0",
        description="One persistent identity across model runtimes and agent workers.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.providers = providers
    app.state.memory_gateway = memory_gateway
    app.state.worker_registry = worker_registry
    app.state.chat_service = chat_service
    app.state.reflector = reflector
    app.state.summarizer = summarizer
    app.state.desire_host = desire_host

    if settings.api_token:
        expected_auth = f"Bearer {settings.api_token}".encode()

        @app.middleware("http")
        async def require_api_token(request: Request, call_next):
            if request.url.path.startswith("/api/"):
                supplied = request.headers.get("authorization", "").encode()
                if not secrets.compare_digest(supplied, expected_auth):
                    return JSONResponse(
                        status_code=401,
                        content={"detail": "Missing or invalid API token"},
                        headers={"WWW-Authenticate": "Bearer"},
                    )
            return await call_next(request)

    @app.get("/health/live")
    def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def health_ready() -> dict[str, object]:
        try:
            database.fetchone("SELECT 1 AS ok")
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"status": "ready", "providers": providers.ids()}

    @app.get("/api/providers")
    def list_providers() -> dict[str, list[str]]:
        return {"providers": providers.ids()}

    @app.post("/api/conversations", response_model=ConversationView)
    def create_conversation(payload: ConversationCreate) -> dict:
        return database.create_conversation(payload.title)

    @app.get(
        "/api/conversations/{conversation_id}/messages",
        response_model=list[MessageView],
    )
    def list_messages(conversation_id: str) -> list[dict]:
        try:
            database.get_conversation(conversation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return database.list_messages(conversation_id)

    @app.post("/api/chat", response_model=ChatResponse)
    async def chat(payload: ChatRequest) -> dict:
        try:
            return await chat_service.chat(
                conversation_id=payload.conversation_id,
                request_id=payload.request_id,
                content=payload.content,
                provider_id=payload.provider_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RequestPayloadConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RequestInProgressError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PreviousRequestFailedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/memories", response_model=MemoryView)
    def remember(payload: MemoryCreate) -> dict:
        row = memory_gateway.remember(
            content=payload.content,
            kind=payload.kind,
            importance=payload.importance,
            occurred_at=payload.occurred_at,
            pinned=payload.pinned,
            valence=payload.valence,
            arousal=payload.arousal,
            quotes=payload.quotes,
            scene=payload.scene,
        )
        return _memory_row_to_view(row)

    @app.get("/api/memories/recall", response_model=list[MemoryView])
    def recall_memories(
        q: str = Query(min_length=1, max_length=20_000),
        limit: int = Query(default=5, ge=1, le=20),
        valence: float | None = Query(default=None, ge=-1.0, le=1.0),
        arousal: float | None = Query(default=None, ge=0.0, le=1.0),
    ) -> list[dict]:
        return [
            {
                "id": item.id,
                "kind": item.kind,
                "content": item.content,
                "importance": item.importance,
                "score": item.score,
                "occurred_at": item.occurred_at,
                "created_at": item.created_at,
                "pinned": item.pinned,
                "valence": item.valence,
                "arousal": item.arousal,
                "quotes": item.quotes,
                "scene": item.scene,
                "signals": item.signals,
            }
            for item in memory_gateway.recall(q, limit=limit, mood=(valence, arousal))
        ]

    @app.get("/api/memory/candidates")
    def list_memory_candidates(
        status: str = Query(default="pending", pattern="^(pending|accepted|rejected)$"),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> list[dict]:
        return [_decode_json_columns(row) for row in database.list_memory_candidates(status, limit)]

    @app.post("/api/memory/candidates/{candidate_id}/accept", response_model=MemoryView)
    def accept_memory_candidate(candidate_id: str) -> dict:
        try:
            candidate = database.get_memory_candidate(candidate_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if candidate["status"] != "pending":
            raise HTTPException(status_code=409, detail="Candidate is not pending")
        memory = memory_gateway.remember(
            content=candidate["content"],
            kind=candidate["kind"],
            importance=candidate["importance"],
        )
        try:
            database.resolve_memory_candidate(candidate_id, status="accepted", memory_id=memory["id"])
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _memory_row_to_view(memory)

    @app.post("/api/memory/candidates/{candidate_id}/reject")
    def reject_memory_candidate(candidate_id: str) -> dict:
        try:
            database.get_memory_candidate(candidate_id)
            row = database.resolve_memory_candidate(candidate_id, status="rejected")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _decode_json_columns(row)

    @app.get("/api/memory/segments")
    def list_memory_segments(limit: int = Query(default=20, ge=1, le=200)) -> list[dict]:
        return [_decode_json_columns(row) for row in database.list_memory_segments(limit)]

    @app.get("/api/memory/summaries")
    def list_memory_summaries(
        summary_type: str | None = Query(default=None, pattern="^(daily|weekly|monthly)$"),
        limit: int = Query(default=20, ge=1, le=200),
    ) -> list[dict]:
        return [
            _decode_json_columns(row)
            for row in database.list_memory_summaries(summary_type, limit=limit)
        ]

    @app.post("/api/memory/summaries/run")
    async def run_memory_summaries(payload: SummaryRunRequest) -> dict:
        if summarizer is None:
            raise HTTPException(
                status_code=409,
                detail="Summaries need PERSONA_HUB_MEMORY_MODEL and an OpenAI-compatible endpoint",
            )
        now = datetime.now(timezone.utc)
        end = payload.end or period_floor(now.astimezone(tz), payload.summary_type)
        start = payload.start or end - timedelta(days=LOOKBACK_DAYS[payload.summary_type])
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        stats = await summarizer.run(payload.summary_type, start, end, force=payload.force)
        return stats.to_dict()

    @app.get("/api/desire/state")
    def desire_state() -> dict:
        return desire_host.public_state()

    @app.get("/api/desire/activity")
    def desire_activity(limit: int = Query(default=50, ge=1, le=200)) -> list[dict]:
        return database.list_desire_activity(limit)

    @app.post("/api/rooms")
    def create_room(payload: RoomCreate) -> dict:
        try:
            for participant in payload.participants:
                providers.get(participant.provider_id)
            room = database.create_room(
                payload.title,
                [participant.model_dump() for participant in payload.participants],
            )
            room["participants"] = database.get_room_participants(room["id"])
            return room
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post(
        "/api/rooms/{room_id}/turns", response_model=RoomTurnResponse
    )
    async def submit_room_turn(room_id: str, payload: RoomTurnCreate) -> dict:
        try:
            return await room_service.submit_turn(
                room_id=room_id,
                request_id=payload.request_id,
                content=payload.content,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RequestPayloadConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/workers", response_model=list[WorkerView])
    def list_workers() -> list[dict]:
        return worker_registry.list_public()

    @app.websocket("/worker/ws")
    async def worker_websocket(websocket: WebSocket) -> None:
        await worker_registry.serve(websocket)

    return app


app = create_app()
