# Windows Harness ⊞

The simplest, thinnest harness that gives an LLM complete freedom to complete
virtually any task on a PC.

The agent writes what is missing, mid-task. No framework, no recipes, no rails.
One Python process connected directly to Windows, your real browser, and your
files.

```text
● agent: wants to do something no helper exists for
│
● sees the window and uses raw Windows primitives
│
● writes the missing logic in ordinary Python
│
✓ task complete                                  no app-specific tool added
```

**Your agent now has a PC.**

## Give it to your agent

Paste this into Codex or Claude Code:

```text
Install or upgrade Windows Harness from https://github.com/warmshao/windows-harness
with uv using Python 3.12. Register the skill printed by `windows-harness skill`,
then run `windows-harness doctor`. Finally, verify the harness by capturing one
already-running app without bringing it to the foreground.
```

That is it. The agent installs the package, teaches itself the workflow, checks
the runtime, and verifies the connection.

## Six primitives. The whole desktop.

```bash
windows-harness <<'PY'
frame = win.see("Notepad")
win.key("ctrl+h", app="Notepad")
win.type("Alessia Cara", app="Notepad")
win.click(640, 420, app="Notepad")

item = win.ax.at(640, 420, app="Notepad")
win.script("(Get-Process notepad).Count")

print(list(Path.home().iterdir()))
PY
```

Think in `see`, `key`, `type`, `click`, `ax`, and `script`. `Path` and
`subprocess` are ready in the same Python process.

There are no Spotify tools, Slack tools, or Excel tools. The model gets raw
primitives and writes the rest.

## How it works

```text
                          one persistent Python process
                                        │
                 ┌──────────────────────┼──────────────────────┐
                 │                      │                      │
              win.*                 browser via CDP     Path / subprocess
                 │                      │                      │
     ┌───────────┼───────────┐     Playwright /        files + shell
     │           │           │     browser-use
 PrintWindow  delivery   UI Automation
 screenshots  matrix          │
     │           │       real Chrome
     └───────────┴───────────┘
                 │
        native + Electron + UWP apps
```

- Captures background, occluded, and minimized windows without raising them
- Delivers input through two honest modes (architecture ported from
  trycua/cua, MIT):

  | delivery | transports | disturbance |
  |---|---|---|
  | `background` (default) | synthetic-pen injection, PostMessage | none — never fronts |
  | `foreground` (explicit) | DWM-cloaked takeover + SendInput; pen/message fallback when SendInput is filtered | one ~150 ms flicker |

  Frameworks that silently drop background events are detected up front
  (class-name matrix, extended by locally observed drops) and refused with a
  structured `BackgroundUnavailable` error instead of pretending; the agent
  escalates to `foreground` explicitly. `windows-harness doctor` probes
  whether SendInput itself survives the machine's hook software and reports
  the verdict under `input_health`.

- Draws an animated, click-through pointer without moving your real cursor
- Exposes raw UI Automation when vision is not enough
- Keeps ordinary Python, PowerShell, and the local filesystem within reach

Windows has no public `CGEventPostToPid` equivalent, so the harness is honest
instead of magical: coordinate clicks route through synthetic-pointer input
(accepted by Chromium/WPF/UWP in the background), classic apps through window
messages, and anything impossible is refused with a structured reason rather
than stealing focus. `delivery="foreground"` is the explicit escalation.

## Requirements and privacy

`windows-harness doctor` reports what the runtime needs: an interactive
desktop (not Session 0) and the three Python dependencies. No administrator
rights are required—except to control apps that themselves run elevated
(UIPI blocks message injection across integrity levels, and the harness
reports it rather than fighting it).

Experimental. Windows 10 1809+ / Windows 11. MIT licensed.
