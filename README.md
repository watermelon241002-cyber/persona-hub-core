# Persona Hub Core

**One identity, many model runtimes.**

Persona Hub Core is an API-first reference kernel for keeping one persona continuous across web clients, mobile devices, model providers, Claude Code, Codex, MCP services, and future embodied devices.

The model is replaceable. Identity, conversation history, memory, routing state, and turn ownership live in the Hub.

> Status: `0.1.0-alpha`. This repository is a clean reference implementation extracted from a personal production architecture. It contains no production credentials, private prompts, memories, provider accounts, or user data.

## Why this exists

Most multi-model systems copy a large prompt into every client. That works until the user changes model, opens another device, compacts a CLI session, or loses a provider. Persona Hub uses a different ownership model:

- Persona Hub owns identity, conversations, memory, and orchestration.
- Providers perform one inference and can be replaced.
- Local workers connect outbound over WebSocket and never need a public port.
- Relevant memories are recalled before inference instead of relying on the model to call a search tool.
- Request IDs, persisted results, and replayable events prevent reconnects from becoming duplicate model calls.

## Included in this alpha

- SQLite conversation and message persistence
- Idempotent chat requests
- Stable/dynamic context package builder
- Pluggable provider registry
- Built-in deterministic echo provider for local tests
- Optional OpenAI-compatible provider adapter
- Natural-recall memory gateway with a deterministic local demo embedder
- Outbound worker WebSocket registration and heartbeat protocol
- Sequential agent room: human -> first collaborator -> second collaborator -> human
- Room delivery IDs and duplicate-turn protection
- Health, provider, worker, memory, chat, and room APIs
- Docker, Caddy, systemd, CI, security, and deployment examples
- Full architecture and three-agent-room specifications under `docs/`

## Deliberately not included

- Any real persona prompt or relationship history
- Production memory databases
- API keys, proxy subscriptions, domains, IP addresses, or account cookies
- Computer-wide unrestricted permissions
- Provider-specific bypasses or undocumented authentication flows
- A claim that every roadmap module is production-ready

Voice, desire/heartbeat, Ombre Brain, MCP management, and the full mobile frontend are documented integration surfaces and roadmap modules, not silently mocked features.

## Architecture

```mermaid
flowchart TB
    Client["Web / mobile / device"] --> Hub["Persona Hub"]
    Hub <--> DB["SQLite truth source"]
    Hub --> Context["Context builder"]
    Context --> Memory["Natural recall memory gateway"]
    Hub --> Router["Provider registry"]
    Router --> API["Direct model APIs"]
    Router --> WS["Worker WebSocket"]
    WS <--> Claude["Claude Code worker"]
    WS <--> Codex["Codex worker"]
    Hub --> Room["Sequential agent room"]
```

## Quick start

```bash
git clone https://github.com/watermelon241002-cyber/persona-hub-core.git
cd persona-hub-core
python -m venv .venv
```

Linux/macOS:

```bash
source .venv/bin/activate
pip install -e ".[test]"
cp .env.example .env
persona-hub
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -e ".[test]"
Copy-Item .env.example .env
persona-hub
```

Open:

- API docs: <http://127.0.0.1:18080/docs>
- Liveness: <http://127.0.0.1:18080/health/live>
- Readiness: <http://127.0.0.1:18080/health/ready>

The default `echo` provider requires no network or API key.

## Minimal API walkthrough

Create a conversation:

```bash
curl -X POST http://127.0.0.1:18080/api/conversations \
  -H "Content-Type: application/json" \
  -d '{"title":"Demo"}'
```

Chat with an idempotency key:

```bash
curl -X POST http://127.0.0.1:18080/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "conversation_id":"<CONVERSATION_ID>",
    "request_id":"demo-request-001",
    "content":"Remember that my demo color is green.",
    "provider_id":"echo"
  }'
```

Sending the exact same `request_id` again returns the persisted result without invoking the provider again.

Store and recall a demo memory:

```bash
curl -X POST http://127.0.0.1:18080/api/memories \
  -H "Content-Type: application/json" \
  -d '{"content":"The demo color is green.","kind":"preference","importance":0.8}'

curl "http://127.0.0.1:18080/api/memories/recall?q=demo%20color&limit=5"
```

## Three-agent room

The alpha implements a deterministic sequential room. The default topology is:

```text
human -> claude role -> codex role -> human
```

The role names do not require those vendors. Each participant points to a registered provider adapter, so the state machine can be tested entirely with fake providers before connecting paid runtimes.

Create a room:

```json
POST /api/rooms
{
  "title": "Architecture review",
  "participants": [
    {
      "agent_id": "claude",
      "display_name": "Claude",
      "provider_id": "echo",
      "position": 1,
      "role_prompt": "Propose the first implementation."
    },
    {
      "agent_id": "codex",
      "display_name": "Codex",
      "provider_id": "echo",
      "position": 2,
      "role_prompt": "Review, complete, or challenge the first response."
    }
  ]
}
```

Submit one human turn:

```json
POST /api/rooms/{room_id}/turns
{
  "request_id": "room-demo-001",
  "content": "Design a safe retry mechanism."
}
```

See [`docs/three-agent-room.md`](docs/three-agent-room.md) for leases, recovery, cursors, and the production rollout plan.

## Using an OpenAI-compatible provider

Set these values in `.env`:

```dotenv
OPENAI_COMPATIBLE_BASE_URL=https://api.example.com/v1
OPENAI_COMPATIBLE_API_KEY=replace-me
OPENAI_COMPATIBLE_MODEL=replace-with-model-id
PERSONA_HUB_DEFAULT_PROVIDER=openai-compatible
```

The adapter is intentionally conservative. A provider appearing in the UI is not proof of successful inference. Verify the final payload, upstream response, persisted message, and duplicate-request behavior.

## Tests

```bash
pytest
```

The test suite covers:

- chat idempotency
- natural memory recall
- provider failure persistence
- fixed room order
- duplicate room-turn protection
- worker registration authorization

## Documentation

- [Full architecture and build guide](docs/architecture.md)
- [Three-agent room design](docs/three-agent-room.md)
- [Worker protocol](docs/worker-protocol.md)
- [Security policy](SECURITY.md)
- [Contribution guide](CONTRIBUTING.md)

## License

Persona Hub Core is licensed under the GNU Affero General Public License v3.0 only. Network deployments that modify this program must make the corresponding source available as required by the license.
