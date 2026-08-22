---
name: windows-harness
description: Control a whole Windows desktop from one persistent Python session with foreground-first input (focus holding, SendInput with pen/message fallbacks), background screenshots, UI Automation, clipboard paste, PowerShell, and filesystem access. Use for native, Electron, browser, dialog, file, or cross-app tasks.
---

# Windows Harness

Use one CLI call per decision point, not per primitive:

```bash
windows-harness <<'PY'
app = "Notepad"
win.see(app)
win.type("hello", app=app)
print(win.see(app))
PY
```

The CLI preloads `win`, `Path`, and `subprocess`. Prefer bounded stdin programs;
reserve `windows-harness repl` for manual exploration and always exit it.

## How to invoke: exactly two forms

One-off questions — use a subcommand (identical in PowerShell, cmd, and bash):

```bash
windows-harness apps                 # app inventory, compact JSON (--all adds system windows)
windows-harness see "CC Switch"      # one screenshot -> JSON with its "path"
windows-harness state "Notepad"      # UIA tree as JSON (--screenshot adds image)
```

Anything multi-step — write a `.py` file, then run it. No quoting or encoding
traps in any shell; this is always the most reliable channel:

```bash
windows-harness run task.py          # win, Path, subprocess preloaded
```

Write generated task scripts to the harness scripts dir (printed by
`windows-harness doctor` as `scripts_dir`, default
`%USERPROFILE%\.windows-harness\scripts`); `run` resolves bare filenames
there, so generated scripts never pollute the caller's working directory.

Never use `<<'PY'` heredocs outside bash; never enumerate windows through
hand-rolled C#/PowerShell — `win.list_apps()` already did it. Screenshots
land in `%TEMP%\windows-harness-*.png` and persist: the returned `"path"`
stays readable after the CLI exits, so just open it. Set
`WINDOWS_HARNESS_OVERLAY=off` to skip the pointer renderer for minimum
latency.

## Delivery: foreground first

Every input primitive takes `delivery="foreground"` (default) or
`delivery="background"`.

| mode | transports | disturbance |
|---|---|---|
| `foreground` (default) | front the target + SendInput; pen/message fallback when hook software swallows SendInput | the target comes to the front and STAYS (`hold=True`) until `win.release()` |
| `background` | synthetic-pen injection, PostMessage, UIA patterns | none — never fronts, never moves the cursor |

Why foreground is the default: focus-driven UI (search boxes with suggestion
popups, context menus, IME candidate windows) closes the instant its window
loses the foreground, and CEF off-screen / XAML input stacks only accept
message input while truly foreground and focused. Holding the foreground
across a burst turns "sometimes works" into a procedure.

- `win.release()` hands the foreground back to the user's previous window.
  Call it when a task burst is done.
- For one quick invisible action use `delivery="foreground", hold=False`
  (cloaked ~150 ms takeover, restored after) or `delivery="background"`.
- Foreground clicks use SendInput mouse events and do NOT summon the Windows
  touch keyboard; synthetic pen/touch injection (background clicks) can pop
  it up and wreck the focus chain — avoid pen taps into text fields.
- `background` results carry `"mode": "pen" | "message"` plus
  `"verified": false` when the effect cannot be confirmed. When a framework's
  input stack silently drops background events the call raises
  `BackgroundUnavailable` with a structured `code`; redo the SAME action with
  default foreground delivery — do not probe more background transports.
- `windows-harness doctor` reports `input_health.sendinput`: `ok`,
  `swallowed` (hook software such as audio enhancers or overlay recorders is
  eating injected events — suspects under `possible_injectors`), or
  `unknown`. When SendInput is swallowed, foreground actions automatically
  fall back to pen/message transports against the now-foreground target.

## Use the small surface

Think in seven verbs: `see`, `key`, `type`, `click`, `paste`, `ax`, `script`
— plus two inventory calls: `win.list_apps()` (processes with their window
titles) and `win.windows(app)` (one app's windows).

```python
frame = win.see("Notepad")
win.key("ctrl+s", app="Notepad")
win.type("Alessia Cara", app="Notepad")                    # type at the focused field
win.type("Alessia Cara", app="Notepad", x=640, y=420)      # click the field, then type
win.click(640, 420, app="Notepad")
win.click(640, 420, app="Notepad", clicks=2)               # double click
win.click(640, 420, app="Notepad", button="right")         # context menu
win.drag(200, 300, 500, 600, app="Notepad")                # press, glide, release
win.scroll(-720, app="Notepad")                            # wheel at window center (or x/y)
win.hover(640, 420, app="Notepad")                         # really hover -> tooltip appears
win.paste("Alessia Cara", app="Notepad")                   # clipboard paste (see ladder)
win.release()                                              # hand the foreground back

item = win.ax.at(640, 420, app="Notepad")
win.ax.perform(item["element_index"], "invoke")
win.ax.click(item["element_index"])      # click the element's center — no coordinates
win.ax.hover(item["element_index"])      # hover it so its tooltip fires

win.script("Get-Process | Select-Object -First 3 Name")
```

`win.click()` is raw coordinate input; it never guesses a UIA action. For
semantic actions use `win.ax.at()` + `win.ax.perform()`, and fill text fields
with `win.ax.set_value()` (verified by read-back) when an element exposes a
ValuePattern — no keystrokes at all. `win.ax.click()` / `win.ax.hover()` act
on the element's own BoundingRectangle center: coordinates never leave the
harness, so no coordinate-space mismatch is possible — prefer them over
copying frame values into `win.click()` yourself.

`win.hover(x, y)` delivers a real hover (physical cursor, or pen hover when
SendInput is filtered) so tooltips and hover-only UI appear; `win.move(x, y)`
only animates the virtual pointer and delivers no input. Hover is
coordinate-routed — the target needs to be unoccluded, not foreground — and
the cursor stays put so the tooltip survives long enough for a `win.see()`.

## Text input ladder

1. `win.ax.set_value()` when the element exposes a ValuePattern — verified by
   read-back and works fully in the background.
2. `win.type(text, x=..., y=...)` — focuses the field and types in one call.
3. `win.paste(text)` — the clipboard route. Setting the clipboard is a plain
   data handoff no hook software can filter; only the paste trigger needs
   input (SendInput Ctrl+V, or a posted WM_PASTE when SendInput is filtered —
   never a posted Ctrl+V, whose modifier never reaches GetKeyState). Focus
   the target field first (a click, or `win.type` with x/y).
4. If the paste trigger also fails: right-click the field and click the
   context menu's Paste item (pen injection can drive the menu).
5. When `doctor` reports `input_health.sendinput == "swallowed"`, tell the
   user which processes (`possible_injectors`) are eating injected input —
   removing the hook software is the real fix.

Replacing a field's existing text: there is no reliable select-all keystroke
on hook-filtered machines (Ctrl+A is a combo and refuses; CEF ignores a
posted Backspace). The dependable sequence is a triple click —
`win.click(x, y, clicks=3)` selects the field's text — then `win.type` or
`win.paste`, which replaces the selection. On machines with healthy
SendInput, `win.key("ctrl+a")` first is fine too.

Bare keys vs combos: when SendInput is swallowed, `win.key("enter")` and
other modifier-less keys still work (posted to the foreground target); only
combos like `ctrl+a` refuse, honestly.

## Coordinates

`coordinate_space` accepts:

- `'screenshot'` (default) — pixels of the latest `win.see()` image; the
  returned `scale_x`/`scale_y` map them to the window.
- `'normalized'` — a 0..1000 grid over the client area of the window from the
  latest `win.see()`, independent of screenshot resolution and DPI; use
  directly when the model outputs normalized coordinates:

  ```python
  frame = win.see("Notepad")                          # anchors the window
  win.click(500, 48, app="Notepad", coordinate_space="normalized")
  ```

  The window's origin is resolved live at click time, so the window may move
  between `see()` and `click()`; only a resize invalidates the grid — take a
  fresh `see()` after any resize.
- `'client'` / `'screen'` — raw window-client or physical screen pixels.

Every positional primitive (`click`, `double/right click`, `drag`, `scroll`
with x/y, `type` with x/y, `hover`) accepts `coordinate_space`; the
normalized grid works for all of them.

When semantic identity matters, prefer element centers from the ax tree
over vision-estimated pixels — they survive layout shifts and need no
scaling at all. ax output reports frames in the pixel space of your latest
`win.see()` screenshot of that window (the `frame` key, alongside
`"frame_space": "screenshot"` in `state`), so they drop straight into the
default coordinate space. Without a matching screenshot the raw physical
screen pixels are reported under `frame_screen` instead — those only pair
with `coordinate_space="screen"`, never the default. Cleanest of all is
keeping numbers out of the loop: `win.ax.click(element_index)` /
`win.ax.hover(element_index)`.

Target apps by exe name when titles can collide — resolution prefers an
exact process-name match (`"Discord.exe"`, or bare `"Discord"`), then an
exact title, and only then substrings, so a window whose title merely
mentions the name (a VS Code tab titled "… Discord …") never hijacks the
query.

## Minimize round trips

- Bundle deterministic, reversible steps into one program, then verify once.
- Stop at a genuine decision boundary: ambiguous identity, new coordinates, an
  irreversible action, or unexpected state.
- Do not screenshot merely to confirm a known shortcut opened a field before
  typing; let the final screenshot verify the whole sequence.
- Poll exact UIA state inside the same Python program when possible.
- Use the cheapest strong end-state check: one screenshot for visible state,
  one exact UIA query for semantic state.

## Choose the lowest useful mode

1. Use `win.script()` for a known exact command (Get-Process, registry reads).
2. Otherwise use `win.see(app)` and vision.
3. Prefer a known keyboard route; use a verified coordinate for a visible,
   low-risk target.
4. Use targeted `win.ax` only when semantic identity or state matters. For
   CEF/Electron custom-drawn UI (chat apps, music players, game launchers)
   the tree fills lazily: `state`/`ax.query`/`ax.at` automatically warm a
   near-empty CEF tree with a brief (usually invisible, cloaked) foreground
   round, so give ax one chance before falling back to pixels. Icon-only
   controls (server/avatar images) often expose only generic names — hover
   them (`win.ax.hover`) and read the tooltip from a `win.see()`. If a dump
   still comes back near-empty with a `note`, go straight to screenshot +
   coordinate clicks instead of spending more rounds on UIA.
5. Use `delivery="background"` only for apps already proven to accept it on
   this machine (classic Win32 controls, UIA patterns) — it is the quiet
   opt-in, not the default to probe with.

After a failed verified burst, switch mode or stop. Never repair uncertainty
with repeated keys, clicks, deletion loops, or bulk input.

## Verify, then teach the matrix

- After a `"verified": false` action, confirm the effect with
  `win.verify_change(app)` — a background pixel diff that costs no focus and
  no vision round trip. Use a full `win.see()` only when you need to know
  WHAT changed, not THAT it changed. Animated windows (video, games) report
  change on their own; verify those semantically through `win.ax` instead.
- When a background action provably had no effect (verify_change negative AND
  the task did not advance), record it once with `win.note_drop(kind)` — e.g.
  `win.note_drop("text_input", app="...")`. Future background calls against
  that window class refuse honestly instead of repeating a dead transport.

## Keep the invariants

- Foreground `hold=True` keeps the target fronted across the whole burst;
  always end a task with `win.release()` (or leave the window fronted
  deliberately and say so).
- Background delivery NEVER activates, raises, or moves the cursor. Occluded
  targets refuse with `background_occluded` rather than being raised.
- ax queries against CEF/Electron windows (`Chrome_WidgetWin*`) whose tree is
  near-empty auto-warm it with a brief cloaked foreground takeover; the
  previous foreground is restored when the warm-up round ends.
- Elevated (Administrator) targets are detected up front (UIPI preflight) and
  refused with `background_uipi_blocked`; run the harness elevated to drive them.
- The animated pointer is click-through and never moves the physical cursor;
  non-held foreground rungs move it briefly and put it back.
- Minimized windows are restored before foreground input and stay restored
  under `hold=True`; non-held foreground rungs re-minimize afterwards.
- Prefer semantic UIA patterns over simulated window chrome: frameworks that
  draw their own titlebar (WinUI3, Electron) may ignore posted WM_CLOSE and
  caption-button Invoke. The framework-neutral close is the WindowPattern —
  `win.ax.raw(i).GetWindowPattern().Close()` — then answer the app's own
  confirmation through `ax.perform(..., "invoke")`.
- XAML apps (Win11 Notepad, Settings, UWP frames) refuse background typing —
  use `win.ax.set_value()` there; WPF refuses background drag and scroll —
  use default foreground delivery.

## Browser tasks

Drive browsers through CDP (Playwright, browser-use, `--remote-debugging-port`)
whenever the task lives inside a web page. Do not substitute OS input for CDP
inside web content; keep these primitives for dialogs, downloads, and
everything outside the page.

Run `windows-harness doctor` to inspect the runtime without changing anything.
