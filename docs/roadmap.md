# Roadmap

## 0.1 reference kernel

- [x] SQLite conversations and messages
- [x] idempotent chat requests
- [x] payload-verified idempotency keys
- [x] optional bearer-token REST auth (required in production)
- [x] provider registry and local echo provider
- [x] OpenAI-compatible adapter
- [x] deterministic demo memory gateway
- [x] stable/dynamic context builder
- [x] worker registration and heartbeat
- [x] sequential room with fake-provider tests

## 0.2 worker execution

- [ ] task dispatch and event stream
- [ ] persisted worker cursors
- [ ] delivery leases and deadlines
- [ ] local Claude Code worker example
- [ ] local Codex worker example
- [ ] reconnect without duplicate inference

## 0.3 production memory

- [ ] pluggable embedding interface
- [ ] Qwen3 embedding adapter example
- [ ] embedding version migrations
- [ ] memory candidate extraction
- [ ] deduplication and review UI
- [ ] rolling conversation summaries

## 0.4 integrations

- [ ] MCP service registry and capability policies
- [ ] Ombre Brain adapter example
- [ ] desire/heartbeat scheduler
- [ ] controlled autonomous activity
- [ ] STT/TTS interfaces
- [ ] device/StackChan protocol

## 0.5 frontend

- [ ] usable conversation UI
- [ ] provider -> channel -> model selector
- [ ] message versions, reroll, and deletion
- [ ] worker and MCP status
- [ ] memory/context inspector
- [ ] sequential room UI
- [ ] mobile and PWA support

Roadmap items are not promises of a release date. Security, idempotency, and data ownership take priority over feature count.
