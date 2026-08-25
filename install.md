# Install Windows Harness

## Let your agent do it

Paste this into Codex or Claude Code:

```text
Install or upgrade Windows Harness from PyPI with uv using Python 3.12. Register
its agent skill with `windows-harness install-skill`, then run
`windows-harness doctor`. Explain any runtime limitations before changing the
system. Finally, verify the harness by capturing one already-running app without
bringing it to the foreground.
```

## Or install it yourself

```powershell
uv tool install --python 3.12 --upgrade --force windows-harness
windows-harness install-skill
windows-harness doctor
```

Windows Harness requires an interactive Windows 10 1809+ or Windows 11 desktop.
Administrator rights are unnecessary unless the application being controlled is
itself elevated.

Verify with a quiet background capture:

```powershell
windows-harness see "Notepad"
```

Browser Harness is included and exposed as `browser` in the persistent Python
session. It connects lazily on first use.

Anonymous telemetry contains only the CLI command category, success, duration,
package version, OS/architecture, and detected agent client. It never includes
prompts, app names, screenshots, text, scripts, paths, or window titles.

```powershell
windows-harness telemetry disable
```
