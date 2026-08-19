from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import secrets
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import JSONResponse

from .config import Settings
from .context import ContextBuilder
from .db import Database, RequestPayloadConflictError
from .memory import MemoryGateway
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
    WorkerView,
)
from .providers import EchoProvider, OpenAICompatibleProvider, ProviderRegistry
from .services import (
    ChatService,
    PreviousRequestFailedError,
    RequestInProgressError,
    RoomService,
)
from .workers import WorkerRegistry


def create_app(
    settings: Settings | None = None,
    *,
    database: Database | None = None,
    providers: ProviderRegistry | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate_for_production()
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
    )
    chat_service = ChatService(
        database=database,
        context_builder=context_builder,
        providers=providers,
        default_provider=settings.default_provider,
    )
    try:
        persona_text = Path(settings.persona_file).read_text(encoding="utf-8")
    except FileNotFoundError:
        persona_text = "You are a careful, continuous collaborator."
    room_service = RoomService(
        database=database, providers=providers, persona_text=persona_text
    )
    worker_registry = WorkerRegistry(settings.worker_token)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
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
        )
        row.pop("embedding_json", None)
        return row

    @app.get("/api/memories/recall", response_model=list[MemoryView])
    def recall_memories(
        q: str = Query(min_length=1, max_length=20_000),
        limit: int = Query(default=5, ge=1, le=20),
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
            }
            for item in memory_gateway.recall(q, limit=limit)
        ]

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
