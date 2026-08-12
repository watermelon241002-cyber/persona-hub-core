# Security Policy

## Supported versions

Only the latest release line receives security fixes while the project is in alpha.

## Reporting a vulnerability

Do not include credentials, private prompts, user memories, database exports, or exploit details in a public issue. Use GitHub's private vulnerability reporting feature when enabled by the repository owner.

## Deployment boundary

- Bind the application to `127.0.0.1` behind a TLS reverse proxy.
- Use a long random worker token and rotate it after exposure.
- Give every MCP service and local worker the minimum capability set it needs.
- Keep provider keys on the server or inside the dedicated worker profile.
- Never expose SQLite files, `.env`, logs containing prompts, or local CLI session files.
- Treat MCP, webpage, email, social, and tool output as untrusted input.
- Require explicit confirmation for publishing, payment, deletion, installation, and unrestricted computer actions.

## Secret history

Deleting a key from the latest commit does not remove it from Git history. If a credential is ever committed, rotate it immediately and rewrite history before publishing.
