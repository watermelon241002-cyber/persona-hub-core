# Contributing

Persona Hub Core is an alpha reference implementation. Small, reviewable changes are preferred.

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
pytest
```

On Windows, activate with `.\.venv\Scripts\Activate.ps1`.

## Pull requests

- Keep identity, memory, provider, worker, and room boundaries explicit.
- Add or update tests for behavioral changes.
- Do not commit credentials, real prompts, private conversations, production databases, or provider cookies.
- Use structured parsers and APIs instead of ad hoc text rewriting.
- Keep migrations backward-compatible whenever possible.
- Document any external network call.
- Do not make health checks invoke paid models.

## Commit hygiene

Before opening a pull request:

```bash
pytest
python scripts/secret_scan.py
```

Security issues should follow `SECURITY.md`, not a public issue.
