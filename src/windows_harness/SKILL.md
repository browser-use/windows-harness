---
name: windows-harness
description: Control a whole Windows desktop from one persistent Python session with background screenshots, cua-style background input delivery (synthetic-pen injection, UIA patterns, window messages), a cloaked foreground escalation, an animated virtual pointer, targeted UI Automation, PowerShell, and filesystem access. Use for native, Electron, browser, dialog, file, or cross-app tasks while keeping the user's focus and cursor untouched wherever possible.
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

## Minimize round trips

- Bundle deterministic, reversible steps into one program, then verify once.
- Stop at a genuine decision boundary: ambiguous identity, new coordinates, an
  irreversible action, or unexpected state.
- Do not screenshot merely to confirm a known shortcut opened a field before
  typing; let the final screenshot verify the whole sequence.
- Poll exact UIA state inside the same Python program when possible.
- Use the cheapest strong end-state check: one screenshot for visible state,
  one exact UIA query for semantic state.

## Delivery: two modes, one contract

Every input primitive takes `delivery="background"` (default) or
`delivery="foreground"`.

| mode | transports | disturbance |
|---|---|---|
| `background` | synthetic-pen injection, PostMessage, UIA patterns | none — never fronts, never moves the cursor |
| `foreground` | cloaked SetForegroundWindow + SendInput; pen/message fallback when SendInput is filtered | one ~150 ms flicker, cursor restored |

- `background` result carries `"mode": "pen" | "message"`, plus
  `"verified": false` when the effect cannot be confirmed.
- `windows-harness doctor` probes input health and reports
  `input_health.sendinput`: `ok`, `swallowed` (hook software such as audio
  enhancers or overlay recorders is eating injected events — possible
  culprits listed under `possible_injectors`), or `unknown`. When SendInput
  is swallowed, `foreground` actions automatically fall back to pen/message
  transports against the now-foreground target (`"mode": "foreground-pen" |
  "foreground-message"`); only key combos refuse, with the reason.
- When a framework's input stack silently drops background events, the call
  raises `BackgroundUnavailable` with `code`, `target_class`, and
  `escalation: "foreground"`. Retry the SAME action with
  `delivery="foreground"` exactly then — not preemptively. Foregrounding on a
  guess steals the user's focus for nothing.
- `foreground` results carry `"cloaked"`: whether the takeover was hidden.
- Every result reports `focus` — the harness repairs the user's foreground
  automatically if anything displaced it.

## Use the small surface

Think in six verbs: `see`, `key`, `type`, `click`, `ax`, `script`.

```python
frame = win.see("Notepad")
win.key("ctrl+s", app="Notepad")
win.type("Alessia Cara", app="Notepad")
win.click(640, 420, app="Notepad")

item = win.ax.at(640, 420, app="Notepad")
win.ax.perform(item["element_index"], "invoke")

win.script("Get-Process | Select-Object -First 3 Name")
```

`win.click()` is raw coordinate input; it never guesses a UIA action. For
semantic actions use `win.ax.at()` + `win.ax.perform()`, and fill text fields
with `win.ax.set_value()` (verified by read-back) when an element exposes a
ValuePattern — no keystrokes at all.

## Choose the lowest useful mode

1. Use `win.script()` for a known exact command (Get-Process, registry reads).
2. Otherwise use `win.see(app)` and vision.
3. Prefer a known keyboard route; use a verified coordinate for a visible,
   low-risk target.
4. Use targeted `win.ax` only when semantic identity or state matters.

After a failed verified burst, switch mode or stop. Never repair uncertainty
with repeated keys, clicks, deletion loops, or bulk input.

## Verify, then teach the matrix

- After a `"verified": false` action, confirm the effect with
  `win.verify_change(app)` — a background pixel diff that costs no focus and
  no vision round trip. Use a full `win.see()` only when you need to know
  WHAT changed, not THAT it changed. Animated windows (video, games) report
  change on their own; verify those semantically through `win.ax` instead.
- When an action provably had no effect (verify_change negative AND the task
  did not advance), record it once with `win.note_drop(kind)` — e.g.
  `win.note_drop("text_input", app="...")`. Future background calls against
  that window class refuse honestly instead of repeating a dead transport.
  `doctor` shows the recorded drops.

## Keep the invariants

- Background delivery NEVER activates, raises, or moves the cursor. Occluded
  targets refuse with `background_occluded` rather than being raised.
- Elevated (Administrator) targets are detected up front (UIPI preflight) and
  refused with `background_uipi_blocked`; run the harness elevated to drive them.
- The animated pointer is click-through and never moves the physical cursor;
  foreground rungs move it briefly and put it back.
- Minimized windows are restored under the cloak and re-minimized afterwards.
- Prefer semantic UIA patterns over simulated window chrome: frameworks that
  draw their own titlebar (WinUI3, Electron) may ignore posted WM_CLOSE and
  caption-button Invoke. The framework-neutral close is the WindowPattern —
  `win.ax.raw(i).GetWindowPattern().Close()` — then answer the app's own
  confirmation through `ax.perform(..., "invoke")`.
- Screenshot coordinates come from the latest `win.see()` and preserve client
  bounds and DPI scaling. `coordinate_space` accepts `'screenshot'`,
  `'client'`, or `'screen'`.

## Browser tasks

Drive browsers through CDP (Playwright, browser-use, `--remote-debugging-port`)
whenever the task lives inside a web page. Do not substitute OS input for CDP
inside web content; keep these primitives for dialogs, downloads, and
everything outside the page.

Run `windows-harness doctor` to inspect the runtime without changing anything.
