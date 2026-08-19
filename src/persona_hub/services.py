from __future__ import annotations

import json

from .context import ContextBuilder
from .db import Database
from .providers import GenerationRequest, ProviderRegistry


class RequestInProgressError(RuntimeError):
    pass


class PreviousRequestFailedError(RuntimeError):
    pass


class ChatService:
    def __init__(
        self,
        *,
        database: Database,
        context_builder: ContextBuilder,
        providers: ProviderRegistry,
        default_provider: str,
    ):
        self.database = database
        self.context_builder = context_builder
        self.providers = providers
        self.default_provider = default_provider

    async def chat(
        self,
        *,
        conversation_id: str,
        request_id: str,
        content: str,
        provider_id: str | None,
    ) -> dict:
        selected_provider = provider_id or self.default_provider
        request, replayed = self.database.begin_chat_request(
            request_id=request_id,
            conversation_id=conversation_id,
            provider_id=selected_provider,
            content=content,
        )

        if replayed:
            if request["state"] == "completed":
                return self._response(request, replayed=True)
            if request["state"] == "failed":
                raise PreviousRequestFailedError(
                    "This request failed previously. Retry with a new request_id."
                )
            raise RequestInProgressError(
                "This request already exists and is still in progress."
            )

        try:
            package = self.context_builder.build(conversation_id, content)
            provider = self.providers.get(selected_provider)
            result = await provider.generate(
                GenerationRequest(
                    system_prompt=package.system_prompt,
                    messages=package.messages,
                    metadata={
                        "conversation_id": conversation_id,
                        "request_id": request_id,
                    },
                )
            )
            request = self.database.complete_chat_request(
                request_id=request_id,
                content=result.text,
                provider_id=selected_provider,
                model_id=result.model_id,
                recalled_memory_ids=[item.id for item in package.recalled_memories],
            )
            return self._response(request, replayed=False)
        except Exception as exc:
            self.database.fail_chat_request(
                request_id,
                {"type": type(exc).__name__, "message": str(exc)},
            )
            raise

    def _response(self, request: dict, *, replayed: bool) -> dict:
        if not request["assistant_message_id"]:
            raise RuntimeError("Completed request has no assistant message")
        return {
            "request_id": request["request_id"],
            "state": request["state"],
            "replayed": replayed,
            "user_message": self.database.get_message(request["user_message_id"]),
            "assistant_message": self.database.get_message(
                request["assistant_message_id"]
            ),
            "recalled_memory_ids": json.loads(
                request["recalled_memory_ids_json"] or "[]"
            ),
        }


class RoomService:
    def __init__(
        self,
        *,
        database: Database,
        providers: ProviderRegistry,
        persona_text: str,
    ):
        self.database = database
        self.providers = providers
        self.persona_text = persona_text

    async def submit_turn(
        self, *, room_id: str, request_id: str, content: str
    ) -> dict:
        turn, replayed = self.database.begin_room_turn(
            room_id=room_id, request_id=request_id, content=content
        )
        if replayed:
            return {
                "room_id": room_id,
                "turn_id": turn["id"],
                "request_id": request_id,
                "state": turn["state"],
                "replayed": True,
                "messages": self.database.list_room_messages(turn["id"]),
            }

        messages = [{"role": "user", "content": content}]
        participants = self.database.get_room_participants(room_id)
        try:
            for participant in participants:
                delivery_request_id = (
                    f"agent-room:{room_id}:{turn['id']}:{participant['agent_id']}:1"
                )
                delivery = self.database.create_delivery(
                    room_id=room_id,
                    turn_id=turn["id"],
                    agent_id=participant["agent_id"],
                    request_id=delivery_request_id,
                )
                self.database.set_delivery_state(delivery["id"], "claimed")
                provider = self.providers.get(participant["provider_id"])
                # The role prompt must live in the system prompt: metadata is
                # advisory and real provider adapters ignore it.
                role_prompt = (participant["role_prompt"] or "").strip()
                system_prompt = self.persona_text
                if role_prompt:
                    system_prompt = (
                        f"{self.persona_text}\n\n[Room role]\n{role_prompt}"
                    )
                result = await provider.generate(
                    GenerationRequest(
                        system_prompt=system_prompt,
                        messages=messages,
                        metadata={
                            "room_id": room_id,
                            "turn_id": turn["id"],
                            "agent_id": participant["agent_id"],
                            "role_prompt": participant["role_prompt"],
                        },
                    )
                )
                self.database.append_room_message(
                    room_id=room_id,
                    turn_id=turn["id"],
                    author_type="agent",
                    author_id=participant["agent_id"],
                    display_name=participant["display_name"],
                    content=result.text,
                )
                self.database.set_delivery_state(delivery["id"], "completed")
                messages.append({"role": "assistant", "content": result.text})
            self.database.complete_room_turn(room_id, turn["id"])
        except Exception as exc:
            if "delivery" in locals():
                self.database.set_delivery_state(
                    delivery["id"],
                    "failed",
                    {"type": type(exc).__name__, "message": str(exc)},
                )
            self.database.fail_room_turn(room_id, turn["id"])
            raise

        return {
            "room_id": room_id,
            "turn_id": turn["id"],
            "request_id": request_id,
            "state": "completed",
            "replayed": False,
            "messages": self.database.list_room_messages(turn["id"]),
        }
