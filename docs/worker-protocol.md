# Worker protocol

Persona Hub workers make an outbound WebSocket connection to the Hub. A laptop or desktop does not need a public IP, port forwarding, or a tunnel.

## Endpoint

```text
wss://persona.example.com/worker/ws
```

The alpha server implements registration, authorization, heartbeat, replacement of duplicate worker IDs, and public status views. Task dispatch, event replay, and leases are the next protocol milestone.

## Registration

The first message must be:

```json
{
  "type": "worker.register",
  "worker_id": "local-claude-01",
  "worker_type": "claude_code",
  "protocol_version": 1,
  "capabilities": ["chat", "stream", "tools"],
  "version": "1.0.0",
  "auth": "<SHORT_LIVED_WORKER_TOKEN>"
}
```

Success:

```json
{
  "type": "worker.registered",
  "worker_id": "local-claude-01"
}
```

Close codes:

| Code | Meaning |
|---|---|
| `4400` | malformed registration |
| `4401` | invalid worker token |
| `4409` | connection superseded by the same worker ID |

## Heartbeat

Worker:

```json
{"type":"worker.heartbeat"}
```

Hub:

```json
{"type":"worker.heartbeat.ack","at":"2026-01-01T00:00:00+00:00"}
```

Recommended production values:

- heartbeat every 15 seconds
- offline after 45 seconds without heartbeat
- reconnect with capped exponential backoff and jitter
- restore a persisted cursor after reconnect

## Task dispatch extension

Hub to worker:

```json
{
  "type": "task.dispatch",
  "task_id": "task_uuid",
  "request_id": "conversation:message:attempt-1",
  "conversation_id": "conversation_uuid",
  "provider_profile": "claude-chat-profile",
  "model": "model-id",
  "prompt": "final context package",
  "timeout_ms": 180000,
  "metadata": {
    "source": "chat",
    "stream": true
  }
}
```

Worker events:

```json
{"type":"task.accepted","task_id":"task_uuid"}
{"type":"task.delta","task_id":"task_uuid","seq":1,"text":"first chunk"}
{"type":"task.delta","task_id":"task_uuid","seq":2,"text":"second chunk"}
{"type":"task.completed","task_id":"task_uuid","usage":{"input_tokens":1234,"output_tokens":456}}
```

Structured failure:

```json
{
  "type": "task.failed",
  "task_id": "task_uuid",
  "error": {
    "code": "UPSTREAM_429",
    "message": "Provider capacity limit",
    "retryable": true
  }
}
```

## Idempotency

`request_id` identifies one logical paid model invocation. A worker and Hub must both persist it.

- Existing `queued`, `claimed`, or `streaming`: join the existing request.
- Existing `completed`: replay the persisted result.
- Existing `failed` or `timed_out`: require an explicit retry with a new attempt.
- A status probe must never call the model.

## Leases

Production dispatch should persist:

```text
claimed_at
lease_until
hard_deadline
last_event_seq
```

Heartbeat renews the lease, but never extends `hard_deadline`. Expiry makes the delivery inspectable; it must not silently issue another paid request.

## Local runtime isolation

Each worker should have its own:

- working directory
- provider profile
- environment variables
- CLI session namespace
- logs
- capability allowlist

This prevents a developer changing an interactive CLI route from silently changing the provider used by a persistent frontend worker.

## Security

- Use `wss://` in production.
- Rotate worker tokens after exposure.
- Never send provider keys to the browser.
- Never include hidden reasoning in task result events.
- Require explicit confirmation for destructive computer actions.
- Treat task content and tool output as untrusted data.
