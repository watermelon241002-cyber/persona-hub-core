from __future__ import annotations

from starlette.websockets import WebSocketDisconnect


def test_worker_registers_and_heartbeats(client):
    with client.websocket_connect("/worker/ws") as websocket:
        websocket.send_json(
            {
                "type": "worker.register",
                "worker_id": "worker-test-01",
                "worker_type": "claude_code",
                "capabilities": ["chat", "stream"],
                "version": "test",
                "auth": "test-worker-token",
            }
        )
        assert websocket.receive_json() == {
            "type": "worker.registered",
            "worker_id": "worker-test-01",
        }
        workers = client.get("/api/workers").json()
        assert workers[0]["worker_id"] == "worker-test-01"

        websocket.send_json({"type": "worker.heartbeat"})
        assert websocket.receive_json()["type"] == "worker.heartbeat.ack"


def test_worker_rejects_bad_token(client):
    with client.websocket_connect("/worker/ws") as websocket:
        websocket.send_json(
            {
                "type": "worker.register",
                "worker_id": "unauthorized-worker",
                "auth": "wrong-token",
            }
        )
        try:
            websocket.receive_json()
        except WebSocketDisconnect as exc:
            assert exc.code == 4401
        else:
            raise AssertionError("Unauthorized worker connection stayed open")
