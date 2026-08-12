from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class WorkerConnection:
    worker_id: str
    worker_type: str
    capabilities: list[str]
    version: str
    connected_at: str
    last_seen_at: str
    websocket: WebSocket

    def public_view(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "worker_type": self.worker_type,
            "capabilities": list(self.capabilities),
            "version": self.version,
            "connected_at": self.connected_at,
            "last_seen_at": self.last_seen_at,
        }


class WorkerRegistry:
    def __init__(self, token: str):
        self.token = token
        self._workers: dict[str, WorkerConnection] = {}

    async def serve(self, websocket: WebSocket) -> None:
        await websocket.accept()
        worker_id: str | None = None
        try:
            registration = await websocket.receive_json()
            if registration.get("type") != "worker.register":
                await websocket.close(code=4400, reason="registration required")
                return
            if registration.get("auth") != self.token:
                await websocket.close(code=4401, reason="unauthorized")
                return

            worker_id = str(registration.get("worker_id", "")).strip()
            if not worker_id:
                await websocket.close(code=4400, reason="worker_id required")
                return
            now = utc_now()
            connection = WorkerConnection(
                worker_id=worker_id,
                worker_type=str(registration.get("worker_type", "generic")),
                capabilities=[str(value) for value in registration.get("capabilities", [])],
                version=str(registration.get("version", "unknown")),
                connected_at=now,
                last_seen_at=now,
                websocket=websocket,
            )
            previous = self._workers.get(worker_id)
            if previous is not None:
                await previous.websocket.close(code=4409, reason="superseded")
            self._workers[worker_id] = connection
            await websocket.send_json(
                {"type": "worker.registered", "worker_id": worker_id}
            )

            try:
                while True:
                    message = await websocket.receive_json()
                    connection.last_seen_at = utc_now()
                    if message.get("type") == "worker.heartbeat":
                        await websocket.send_json(
                            {
                                "type": "worker.heartbeat.ack",
                                "at": connection.last_seen_at,
                            }
                        )
                    else:
                        await websocket.send_json(
                            {
                                "type": "worker.event.ack",
                                "event_type": message.get("type", "unknown"),
                            }
                        )
            except WebSocketDisconnect:
                pass
        finally:
            if worker_id and self._workers.get(worker_id, None) is not None:
                current = self._workers[worker_id]
                if current.websocket is websocket:
                    self._workers.pop(worker_id, None)

    def list_public(self) -> list[dict[str, Any]]:
        return [worker.public_view() for worker in self._workers.values()]
