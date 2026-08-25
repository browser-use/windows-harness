# Contributing

Windows Harness is intentionally low-level. Prefer exposing a raw Windows
primitive over adding an app-specific helper or workflow.

```powershell
uv sync
uv run ruff check .
uv run pytest
```

Pull requests should preserve three invariants: report delivery failures
honestly, keep coordinate systems explicit, and never send task or UI data
through telemetry.
