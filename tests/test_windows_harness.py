"""Smoke tests. GUI-dependent tests skip without an interactive desktop."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from windows_harness.capture import (  # noqa: E402
    HarnessError,
    anchor_health,
    capture_screen,
    capture_window,
    dpi_health,
    enumerate_windows,
    is_interactive_desktop,
    resolve_hwnd,
    virtual_screen_bounds,
)
from windows_harness.delivery import (  # noqa: E402
    post_message_blocked_by_uipi,
)
from windows_harness.inject import (  # noqa: E402
    _split_scroll_delta,
    _utf16_code_units,
    pack_lparam,
    parse_combo,
    vk_for_key,
    zip_strict,
)
from windows_harness.pointer import POINTER_HOTSPOT, pointer_points  # noqa: E402
from windows_harness.windows import (  # noqa: E402
    _normalize_newlines,
    _text_landed,
)


def test_anchor_health_classifies_window_movement():
    """A moved/resized window turns the frozen anchor into a stale one, but the
    -32000 iconified handoff stays "offscreen" (keep the frozen anchor)."""
    frozen = {"x": 939, "y": 594, "width": 1575, "height": 1050}
    assert anchor_health(frozen, (939, 594, 1575, 1050)) == "ok"
    # Pure translation, size unchanged -> stale.
    assert anchor_health(frozen, (951, 646, 1575, 1050)) == "moved"
    # Sub-tolerance jitter is not a real move.
    assert anchor_health(frozen, (940, 596, 1575, 1050)) == "ok"
    # Client size change -> the screenshot's scale is stale.
    assert anchor_health(frozen, (939, 594, 1500, 1000)) == "resized"
    # The documented iconified/foreground-handoff transient must NOT be a move.
    assert anchor_health(frozen, (-32000, -32000, 1, 1)) == "offscreen"
    assert anchor_health(frozen, (-32000, -32000, 0, 0)) == "offscreen"


def test_dpi_health_classifies_awareness():
    """Only per-monitor awareness is healthy; others are reported as risks."""
    assert dpi_health(2)["ok"] is True
    assert dpi_health(2)["awareness"] == "per_monitor"
    for value in (0, 1):
        assert dpi_health(value)["ok"] is False
    assert dpi_health(-1)["awareness"] == "unknown"
    assert dpi_health(-1)["ok"] is False


def test_pointer_geometry_scales_around_hotspot():
    points = pointer_points()
    assert len(points) == 4
    pressed = pointer_points(pressed=True)
    hot_x, hot_y = POINTER_HOTSPOT
    assert pressed[0] == (hot_x + (points[0][0] - hot_x), hot_y + (points[0][1] - hot_y))
    assert all(px <= qx for (px, _), (qx, _) in zip(pressed, points))


def test_pack_lparam_matches_makelparam():
    assert pack_lparam(100, 200) == (200 << 16) | 100
    assert pack_lparam(0, 0) == 0


def test_vk_and_combo_parsing():
    assert vk_for_key("a") == 0x41
    assert vk_for_key("A") == 0x41
    assert vk_for_key("5") == 0x35
    assert vk_for_key("return") == 0x0D
    base, modifiers = parse_combo("ctrl+shift+n")
    assert base == ord("N")
    assert modifiers == [0x11, 0x10]
    with pytest.raises(HarnessError):
        vk_for_key("f13+")


@pytest.mark.skipif(not is_interactive_desktop(), reason="no interactive desktop")
def test_enumerate_windows_finds_something():
    windows = enumerate_windows()
    assert windows
    assert any(window["visible"] for window in windows)


@pytest.mark.skipif(not is_interactive_desktop(), reason="no interactive desktop")
def test_resolve_hwnd_by_pid_and_miss():
    visible = [w for w in enumerate_windows() if w["visible"] and w["bounds"]]
    if not visible:
        pytest.skip("no visible windows")
    target = visible[0]
    hwnd, info = resolve_hwnd(str(target["pid"]))
    assert hwnd
    assert info["pid"] == target["pid"]
    with pytest.raises(HarnessError):
        resolve_hwnd("::definitely-no-such-window::")


@pytest.mark.skipif(not is_interactive_desktop(), reason="no interactive desktop")
def test_uipi_preflight_runs_on_real_window():
    """Non-elevated harness vs non-elevated target: None means allowed."""
    visible = [w for w in enumerate_windows() if w["visible"] and w["bounds"]]
    if not visible:
        pytest.skip("no visible windows")
    assert post_message_blocked_by_uipi(visible[0]["hwnd"]) is None


@pytest.mark.skipif(not is_interactive_desktop(), reason="no interactive desktop")
def test_capture_window_returns_image_metadata():
    import ctypes

    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if not hwnd:
        visible = [w for w in enumerate_windows() if w["visible"] and w["bounds"]]
        if not visible:
            pytest.skip("nothing to capture")
        hwnd = visible[0]["hwnd"]
    shot = capture_window(hwnd)
    assert shot["image"].width >= 1
    assert shot["scale_x"] > 0
    assert {"x", "y", "width", "height"} <= set(shot["client_bounds"])


def test_utf16_code_units_pairs_surrogates():
    assert _utf16_code_units("A") == [0x41]
    assert _utf16_code_units("中") == [0x4E2D]
    high_low = _utf16_code_units("\U0001F600")
    assert len(high_low) == 2
    assert 0xD800 <= high_low[0] <= 0xDBFF
    assert 0xDC00 <= high_low[1] <= 0xDFFF


def test_split_scroll_delta():
    assert _split_scroll_delta(0, 120) == []
    assert _split_scroll_delta(240, 120) == [120, 120]
    assert _split_scroll_delta(-250, 120) == [-120, -120, -10]


def test_observed_drops_roundtrip(tmp_path, monkeypatch):
    import ctypes

    from windows_harness import delivery

    monkeypatch.setenv("WINDOWS_HARNESS_HOME", str(tmp_path))
    monkeypatch.setattr(delivery, "_OBSERVED_CACHE", None)
    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if not hwnd:
        visible = [w for w in enumerate_windows() if w["visible"] and w["bounds"]]
        if not visible:
            pytest.skip("no window to annotate")
        hwnd = visible[0]["hwnd"]

    class_name = delivery.read_class_name(hwnd)
    drops = delivery.note_observed_drop(hwnd, delivery.TEXT_INPUT)
    assert drops[class_name] == [delivery.TEXT_INPUT]
    assert delivery.would_be_silently_dropped(hwnd, delivery.TEXT_INPUT)
    with pytest.raises(HarnessError):
        delivery.note_observed_drop(hwnd, "not_an_event")


@pytest.mark.skipif(not is_interactive_desktop(), reason="no interactive desktop")
def test_sendinput_health_probe_returns_a_verdict():
    from windows_harness.inject import sendinput_health, suspect_injectors

    assert sendinput_health() in ("ok", "swallowed", "unknown")
    assert isinstance(suspect_injectors(), list)


@pytest.mark.skipif(not is_interactive_desktop(), reason="no interactive desktop")
def test_element_handles_roundtrip():
    from windows_harness.windows import Windows

    win = Windows()
    marker = object()
    index = win._remember_element(marker)
    assert win._element(index) is marker
    second = win._remember_element(object())
    assert second > index
    with pytest.raises(HarnessError):
        win._element(10**9)


@pytest.mark.skipif(not is_interactive_desktop(), reason="no interactive desktop")
def test_verify_change_smoke():
    from windows_harness.windows import Windows

    # Terminal-hosted consoles return a pseudo hwnd that EnumWindows cannot
    # resolve, and the enumeration leads with 1x1 hook junk — pick a window
    # that passes resolve_hwnd's own 40px floor.
    visible = [
        w for w in enumerate_windows()
        if w["visible"] and w["bounds"]
        and w["bounds"][2] - w["bounds"][0] >= 40
        and w["bounds"][3] - w["bounds"][1] >= 40
    ]
    if not visible:
        pytest.skip("nothing to diff")
    result = Windows().verify_change(str(visible[0]["hwnd"]), timeout=0.3)
    assert isinstance(result["changed"], bool)
    assert result["elapsed"] > 0


def test_ax_get_value_falls_back_to_patterns():
    """Controls without a .Value attribute still expose it through patterns."""
    from windows_harness.controls import Accessibility

    class FakePattern:
        Value = "read through the pattern"

    class FakeElement:  # no .Value attribute, like window roots / documents
        def GetValuePattern(self):
            return FakePattern()

        def GetLegacyIAccessiblePattern(self):
            return None

    class Host:
        _elements = {0: FakeElement()}

        def _element(self, index):  # mirrors Windows._element's honest error
            try:
                return self._elements[index]
            except KeyError as exc:
                raise HarnessError(f"Unknown element index {index!r}") from exc

    assert Accessibility(Host()).get(0, "Value") == "read through the pattern"
    with pytest.raises(HarnessError):
        Accessibility(Host()).get(1, "Value")  # unknown index stays honest


def test_ax_set_value_tolerates_newline_normalization():
    """SetValue's verify-after-write must not false-fail on CRLF/CR vs LF."""
    from windows_harness.controls import Accessibility

    class FakePattern:
        def __init__(self):
            self.Value = ""

        def SetValue(self, value):
            # A Windows edit control normalises LF to CRLF on read-back.
            self.Value = value.replace("\n", "\r\n")

    class FakeElement:
        def __init__(self):
            self._pattern = FakePattern()

        def GetValuePattern(self):
            return self._pattern

    class Host:
        def _element(self, index):
            try:
                return self._elements[index]
            except KeyError as exc:
                raise HarnessError(f"Unknown element index {index!r}") from exc

    element = FakeElement()
    host = Host()
    host._elements = {0: element}
    # No exception means the CRLF round-trip was accepted as an exact match.
    Accessibility(host).set_value(0, "line1\nline2")
    assert element._pattern.Value == "line1\r\nline2"


def test_vk_for_key_rejects_non_ascii():
    """Non-ASCII alnum characters must fail loudly, not emit bogus VK codes."""
    from windows_harness.inject import vk_for_key

    with pytest.raises(HarnessError):
        vk_for_key("中")
    assert vk_for_key("n") == 0x4E  # ASCII behaviour is untouched


@pytest.mark.skipif(not is_interactive_desktop(), reason="no interactive desktop")
def test_pen_injection_on_dead_window_reports_missing_window():
    """Regression: ctypes returns int, so `IsWindow(...) is False` never fired."""
    from windows_harness.inject import inject_click_screen

    with pytest.raises(HarnessError, match="no longer exists"):
        inject_click_screen(0xDEADBEEF, 10, 10, button="left", clicks=1)


@pytest.mark.skipif(not is_interactive_desktop(), reason="no interactive desktop")
def test_element_indices_monotonic_and_eviction_honest(monkeypatch):
    """Stale handles never alias fresh elements; evicted ones fail loudly."""
    from windows_harness import windows as windows_module
    from windows_harness.windows import Windows

    win = Windows()
    first = win._remember_element("first")
    second = win._remember_element("second")
    assert second == first + 1

    monkeypatch.setattr(windows_module, "_ELEMENT_CACHE_LIMIT", 8)
    for _ in range(20):
        win._remember_element(object())
    with pytest.raises(HarnessError, match="fresh snapshot"):
        win._element(first)


@pytest.mark.skipif(not is_interactive_desktop(), reason="no interactive desktop")
def test_default_screenshot_path_persists():
    """Auto-generated shots must outlive the producing process — agents read
    the returned path only afterwards. Nothing may delete them proactively."""
    from PIL import Image

    from windows_harness.windows import Windows

    win = Windows()
    output = win._save_shot(Image.new("RGB", (4, 4)), None)
    assert output.exists()
    assert output.name.startswith("windows-harness-")


@pytest.mark.skipif(not is_interactive_desktop(), reason="no interactive desktop")
def test_proof_journal_roundtrip_and_recovery(tmp_path, monkeypatch):
    """A forgotten result["proof"]["path"] is recoverable from the journal:
    newest-first ordering, app/kind filters, and dead-PNG eviction."""
    from pathlib import Path

    from windows_harness.windows import Windows

    monkeypatch.setenv("WINDOWS_HARNESS_HOME", str(tmp_path))
    win = Windows()
    win._last_window = {
        "hwnd": 42,
        "process": "notepad.exe",
        "title": "Untitled - Notepad",
    }

    def fake_proof(name, kind, label):
        png = tmp_path / name
        png.write_bytes(b"png")
        return {
            "path": str(png),
            "kind": kind,
            "label": label,
            "coordinate_space": "screenshot",
            "image": {"x": 1.0, "y": 2.0},
            "screen": {"x": 3.0, "y": 4.0},
        }

    first = fake_proof("first.png", "click", "click")
    second = fake_proof("second.png", "scroll", "scroll down")
    win._journal_proof(first, 42)
    win._journal_proof(second, 42)

    assert (tmp_path / "proofs.jsonl").exists()

    recent = win.proofs()
    assert [entry["path"] for entry in recent] == [second["path"], first["path"]]
    assert recent[0]["app"]["process"] == "notepad.exe"
    assert recent[0]["ts"]

    assert win.last_proof()["path"] == second["path"]
    assert [e["kind"] for e in win.proofs(kind="click")] == ["click"]
    assert len(win.proofs(app="notepad")) == 2
    assert win.proofs(app="calculator") == []

    # A proof whose PNG was cleaned up drops out of the index.
    Path(second["path"]).unlink()
    assert win.last_proof()["path"] == first["path"]


def test_proof_journal_rotation(tmp_path, monkeypatch):
    """The journal is bounded: oversize files keep only the newest lines."""
    import json as json_module

    import windows_harness.windows as windows_module
    from windows_harness.windows import Windows

    monkeypatch.setattr(windows_module, "_JOURNAL_MAX_BYTES", 100)
    monkeypatch.setattr(windows_module, "_JOURNAL_KEEP_LINES", 5)
    journal = tmp_path / "proofs.jsonl"
    journal.write_text(
        "".join(json_module.dumps({"n": i}) + "\n" for i in range(20)),
        encoding="utf-8",
    )
    Windows._rotate_journal(journal)
    lines = journal.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5
    assert json_module.loads(lines[-1])["n"] == 19


@pytest.mark.skipif(not is_interactive_desktop(), reason="no interactive desktop")
def test_cli_exec_runs_snippet(capsys):
    """argv-only invocation must work without any stdin plumbing."""
    from windows_harness.cli import main

    assert main(["exec", "print(len(win.list_apps()))"]) == 0
    out = capsys.readouterr().out.strip()
    assert out.isdigit()
    assert main(["exec", "   "]) == 2


@pytest.mark.skipif(not is_interactive_desktop(), reason="no interactive desktop")
def test_cli_run_executes_script_file(tmp_path, capsys):
    """File-based invocation reads UTF-8 and tolerates a BOM."""
    from windows_harness.cli import main

    script = tmp_path / "task.py"
    script.write_bytes(
        "﻿print(sum(len(a['windows']) for a in win.list_apps()))\n".encode("utf-8")
    )
    assert main(["run", str(script)]) == 0
    assert capsys.readouterr().out.strip().isdigit()

    missing = tmp_path / "nope.py"
    assert main(["run", str(missing)]) == 1


@pytest.mark.skipif(not is_interactive_desktop(), reason="no interactive desktop")
def test_apps_inventory_filters_system_plumbing():
    """Default listing stays small; --all is the raw superset."""
    from windows_harness.capture import list_processes

    clean = list_processes()
    raw = list_processes(include_system=True)
    assert len(clean) <= len(raw)
    junk = {"Default IME", "MSCTFIME UI", "IME", "GDI+ Window"}
    assert all(
        title not in junk
        for entry in clean
        for title in entry["windows"]
    )
    assert all(entry["windows"] for entry in clean)  # no empty entries


def test_click_hint_flags_webview_hosts(monkeypatch):
    """Message-mode clicks on WebView2 hosts carry an actionable hint."""
    from windows_harness import delivery
    from windows_harness import windows as windows_module

    monkeypatch.setattr(delivery, "has_chromium_descendant", lambda hwnd: True)
    hint = windows_module._click_delivery_hint({"mode": "message"}, 1234)
    assert hint and "foreground" in hint
    assert windows_module._click_delivery_hint({"mode": "pen"}, 1234) is None
    monkeypatch.setattr(delivery, "has_chromium_descendant", lambda hwnd: False)
    assert windows_module._click_delivery_hint({"mode": "message"}, 1234) is None


def _isolate_matrix(monkeypatch, *, xaml=False):
    """Neutralize every delivery detector except the one under test."""
    from windows_harness import delivery

    monkeypatch.setattr(delivery, "observed_drops", lambda: {})
    for name in (
        "is_chromium_target_window", "has_chromium_descendant",
        "is_wpf_target_window", "is_tk_target_window",
        "is_winui3_target_window", "is_gtk_target_window",
        "is_vcl_target_window",
    ):
        monkeypatch.setattr(delivery, name, lambda h: False)
    monkeypatch.setattr(delivery, "is_xaml_host_window", lambda h: xaml)
    monkeypatch.setattr(delivery, "_target_is_foreground", lambda h: True)
    return delivery


def test_wpf_refuses_pointer_and_scroll(monkeypatch):
    """WPF drops posted pointer input AND the posted wheel (cua learning)."""
    delivery = _isolate_matrix(monkeypatch)
    monkeypatch.setattr(delivery, "is_wpf_target_window", lambda h: True)
    assert delivery.would_be_silently_dropped(1, delivery.MOUSE_CLICK)
    assert delivery.would_be_silently_dropped(1, delivery.MOUSE_SCROLL)
    assert not delivery.would_be_silently_dropped(1, delivery.TEXT_INPUT)
    assert not delivery.would_be_silently_dropped(1, delivery.KEYSTROKE)


def test_xaml_hosts_refuse_background_keyboard(monkeypatch):
    """XAML islands ignore posted keys/text; clicks stay allowed (CUA-543)."""
    delivery = _isolate_matrix(monkeypatch, xaml=True)
    assert delivery.would_be_silently_dropped(1, delivery.TEXT_INPUT)
    assert delivery.would_be_silently_dropped(1, delivery.KEYSTROKE)
    assert delivery.would_be_silently_dropped(1, delivery.KEY_COMBO)
    assert not delivery.would_be_silently_dropped(1, delivery.MOUSE_CLICK)


def test_double_click_message_gated_by_class_style(monkeypatch):
    """WM_*BUTTONDBLCLK is posted only to classes registered CS_DBLCLKS."""
    from windows_harness import inject

    monkeypatch.setattr(inject, "_get_class_long", lambda h, i: 0x0008)
    assert inject._class_wants_double_click(1234)
    monkeypatch.setattr(inject, "_get_class_long", lambda h, i: 0)
    assert not inject._class_wants_double_click(1234)


@pytest.mark.skipif(not is_interactive_desktop(), reason="no interactive desktop")
def test_focused_descendant_smoke():
    """Multi-thread focus drill-down runs against a real window."""
    from windows_harness.capture import enumerate_windows
    from windows_harness.inject import focused_descendant

    visible = [w for w in enumerate_windows() if w["visible"] and w["bounds"]]
    if not visible:
        pytest.skip("no visible windows")
    result = focused_descendant(visible[0]["hwnd"])
    assert isinstance(result, int) and result >= 0


def test_normalized_coordinates_map_over_client_bounds(monkeypatch):
    """The 0..1000 grid maps onto the last screenshot's client area."""
    from windows_harness import windows as windows_module

    win = windows_module.Windows.__new__(windows_module.Windows)
    win._last_window = {"hwnd": 42}
    win._last_screenshot = {
        "hwnd": 42,
        "scale_x": 1.0,
        "scale_y": 1.0,
        "client_bounds": {"x": 100, "y": 200, "width": 800, "height": 600},
    }
    assert win._screen_point(0, 0, "normalized") == (100.0, 200.0)
    assert win._screen_point(1000, 1000, "normalized") == (900.0, 800.0)
    assert win._screen_point(500, 250, "normalized") == (500.0, 350.0)
    with pytest.raises(HarnessError):
        win._screen_point(1200, 0, "normalized")


def test_hold_keeps_foreground_and_release_restores(monkeypatch):
    """hold=True fronts the target and never restores; release_hold does."""
    from windows_harness import inject

    monkeypatch.setattr(inject, "_HELD_FOREGROUND", None)
    monkeypatch.setattr(inject, "current_foreground", lambda: 0xAAA)
    monkeypatch.setattr(inject, "get_cursor", lambda: (0, 0))
    monkeypatch.setattr(inject, "set_cursor", lambda x, y: None)
    monkeypatch.setattr(inject.user32, "IsIconic", lambda h: False)
    monkeypatch.setattr(inject.user32, "IsWindow", lambda h: True)
    fronted = []
    monkeypatch.setattr(
        inject, "force_foreground", lambda h: fronted.append(h) or True
    )
    with inject.cloaked_focus(0xBBB, hold=True):
        pass
    assert fronted == [0xBBB]  # fronted once, never handed back
    held = inject.release_hold()
    assert held == {"previous": 0xAAA, "target": 0xBBB}
    assert fronted == [0xBBB, 0xAAA]  # release restores the user's window
    assert inject.release_hold() is None  # safe when nothing held


def test_nonheld_focus_restores_immediately(monkeypatch):
    """The classic (hold=False) path keeps its restore-after-action contract."""
    from windows_harness import inject

    monkeypatch.setattr(inject, "_HELD_FOREGROUND", None)
    monkeypatch.setattr(inject, "current_foreground", lambda: 0xAAA)
    monkeypatch.setattr(inject, "get_cursor", lambda: (0, 0))
    monkeypatch.setattr(inject, "set_cursor", lambda x, y: None)
    monkeypatch.setattr(inject.user32, "IsIconic", lambda h: False)
    monkeypatch.setattr(inject, "set_cloak", lambda h, e: False)
    fronted = []
    monkeypatch.setattr(
        inject, "force_foreground", lambda h: fronted.append(h) or True
    )
    with inject.cloaked_focus(0xBBB, cloak=True):
        pass
    assert fronted == [0xBBB, 0xAAA]  # fronted, then restored


@pytest.mark.skipif(not is_interactive_desktop(), reason="no interactive desktop")
def test_clipboard_text_roundtrip():
    """Unicode (incl. CJK) survives the clipboard unchanged."""
    from windows_harness import inject

    original = inject.get_clipboard_text()
    try:
        inject.set_clipboard_text("harness-剪贴板-roundtrip")
        assert inject.get_clipboard_text() == "harness-剪贴板-roundtrip"
    finally:
        inject.set_clipboard_text(original or "")


def test_run_resolves_bare_filenames_in_scripts_dir(tmp_path, monkeypatch, capsys):
    """`windows-harness run foo.py` falls back to the harness scripts dir."""
    from windows_harness import cli

    monkeypatch.setenv("WINDOWS_HARNESS_HOME", str(tmp_path))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "bare_task.py").write_text(
        "print('ran from scripts dir')", encoding="utf-8"
    )
    assert cli.main(["run", "bare_task.py"]) == 0
    assert "ran from scripts dir" in capsys.readouterr().out


def test_post_paste_sends_wm_paste_not_synthetic_ctrl_v(monkeypatch):
    """A posted Ctrl+V degrades to a plain 'v' in CEF (its modifier never
    reaches GetKeyState); the message fallback must post WM_PASTE instead."""
    from windows_harness import inject

    posted = []
    monkeypatch.setattr(inject, "background_focus_window", lambda h: h)
    monkeypatch.setattr(
        inject, "post_message",
        lambda hwnd, msg, wparam, lparam: posted.append(msg),
    )
    result = inject.post_paste(0xBEEF)
    assert posted == [inject.WM_PASTE]
    assert result["mode"] == "message"


def test_background_hover_posts_mousemove_to_deepest_child(monkeypatch):
    from windows_harness import inject

    monkeypatch.setattr(inject.delivery, "post_message_blocked_by_uipi", lambda h: None)
    monkeypatch.setattr(inject.delivery, "would_be_silently_dropped", lambda h, k: False)
    monkeypatch.setattr(inject, "_deepest_child_at", lambda root, x, y: 0xC0)
    monkeypatch.setattr(inject, "screen_to_client", lambda h, x, y: (x - 1, y - 2))
    posted = []
    monkeypatch.setattr(
        inject, "post_message",
        lambda hwnd, msg, wparam, lparam: posted.append((hwnd, msg)),
    )
    result = inject.hover_screen(0xA0, (100, 200), delivery_mode="background")
    assert posted == [(0xC0, inject.WM_MOUSEMOVE)]
    assert result["target_hwnd"] == 0xC0


def test_foreground_key_bare_key_falls_back_but_combo_refuses(monkeypatch):
    """Swallowed SendInput: bare keys post to the foreground target; only
    combos refuse (posted modifiers never reach GetKeyState)."""
    from contextlib import contextmanager

    from windows_harness import inject

    @contextmanager
    def fake_focus(hwnd, *, cloak=True, hold=False):
        yield False

    monkeypatch.setattr(inject, "cloaked_focus", fake_focus)
    monkeypatch.setattr(inject, "_confirmed_foreground", lambda t, a: None)
    monkeypatch.setattr(inject, "sendinput_healthy", lambda: False)
    posted = []
    monkeypatch.setattr(
        inject, "post_key",
        lambda t, k: posted.append(k) or {"mode": "message", "verified": False},
    )
    result = inject.foreground_key(1, "enter", cloak=True)
    assert posted == ["enter"]
    assert result["mode"] == "foreground-message"
    with pytest.raises(inject.ForegroundError):
        inject.foreground_key(1, "ctrl+a", cloak=True)


def test_annotate_stamps_markers_onto_image():
    """The action-proof renderer draws a reticle and a direction arrow."""
    from PIL import Image

    from windows_harness.annotation import annotate

    image = Image.new("RGB", (400, 300), (240, 240, 240))
    annotate(
        image,
        [
            {"kind": "click", "x": 100, "y": 100, "label": "click"},
            {"kind": "scroll", "x": 200, "y": 100, "delta_y": 360, "delta_x": 0},
        ],
    )
    px = image.load()
    assert px[100, 100] == (255, 45, 45)          # click reticle centre
    assert px[200, 100 - 40] == (255, 45, 45)     # up arrow (positive delta_y)


def test_scroll_arrow_points_with_wheel_sign():
    """Positive delta_y = scroll up = arrow above the anchor; negative = down."""
    from PIL import Image

    from windows_harness.annotation import annotate

    down = Image.new("RGB", (300, 300), (240, 240, 240))
    annotate(down, [{"kind": "scroll", "x": 150, "y": 150, "delta_y": -360, "delta_x": 0}])
    px = down.load()
    assert px[150, 150] == (255, 45, 45)
    assert px[150, 150 + 30] == (255, 45, 45)     # below the anchor
    assert px[150, 150 - 30] == (240, 240, 240)   # nothing above for a down scroll


def test_annotate_drag_draws_start_and_path():
    """A drag is a blue line from the start reticle to the end square."""
    from PIL import Image

    from windows_harness.annotation import annotate

    image = Image.new("RGB", (300, 300), (240, 240, 240))
    annotate(image, [{"kind": "drag", "x": 60, "y": 200, "end_x": 220, "end_y": 220, "label": "drag"}])
    px = image.load()
    assert px[60, 200] == (60, 140, 255)          # start reticle centre
    assert px[140, 210] == (60, 140, 255)         # mid-glide line


def test_annotate_proof_maps_screen_point_to_image(tmp_path):
    """The proof reuses the anchored screenshot and returns a readable path."""
    from PIL import Image

    from windows_harness.windows import Windows

    win = Windows.__new__(Windows)
    win._proof_enabled = True
    shot = tmp_path / "shot.png"
    Image.new("RGB", (300, 200), (200, 200, 200)).save(shot)
    win._last_screenshot = {
        "hwnd": 42,
        "path": str(shot),
        "width": 300,
        "height": 200,
        "scale_x": 1.0,
        "scale_y": 1.0,
        "client_bounds": {"x": 10, "y": 20, "width": 300, "height": 200},
    }
    proof = win._annotate_proof(
        42, (110.0, 120.0), kind="click", label="click",
        coordinate_space="screenshot", annotate=True,
    )
    assert os.path.exists(proof["path"])
    assert proof["image"] == {"x": 100.0, "y": 100.0}
    assert proof["screen"] == {"x": 110.0, "y": 120.0}
    assert proof["kind"] == "click"
    # The anchored source screenshot is untouched; the proof is a new file.
    assert proof["path"] != str(shot)


def test_annotate_proof_respects_flags(tmp_path):
    """annotate=False or a disabled instance returns no proof path."""
    from PIL import Image

    from windows_harness.windows import Windows

    win = Windows.__new__(Windows)
    win._proof_enabled = True
    shot = tmp_path / "shot.png"
    Image.new("RGB", (300, 200), (200, 200, 200)).save(shot)
    win._last_screenshot = {
        "hwnd": 42, "path": str(shot), "width": 300, "height": 200,
        "scale_x": 1.0, "scale_y": 1.0,
        "client_bounds": {"x": 10, "y": 20, "width": 300, "height": 200},
    }
    assert win._annotate_proof(
        42, (110.0, 120.0), kind="click", label="click",
        coordinate_space="screenshot", annotate=False,
    ) is None
    win._proof_enabled = False
    assert win._annotate_proof(
        42, (110.0, 120.0), kind="click", label="click",
        coordinate_space="screenshot", annotate=True,
    ) is None


def test_proof_env_flag(monkeypatch):
    """WINDOWS_HARNESS_PROOF turns the proof renderer off globally."""
    from windows_harness import windows as windows_module

    monkeypatch.delenv("WINDOWS_HARNESS_PROOF", raising=False)
    assert windows_module._env_bool("WINDOWS_HARNESS_PROOF", default=True) is True
    monkeypatch.setenv("WINDOWS_HARNESS_PROOF", "off")
    assert windows_module._env_bool("WINDOWS_HARNESS_PROOF", default=True) is False
    monkeypatch.setenv("WINDOWS_HARNESS_PROOF", "0")
    assert windows_module._env_bool("WINDOWS_HARNESS_PROOF", default=True) is False


def test_action_label_helpers():
    """Click/scroll labels describe the button, count, and wheel direction."""
    from windows_harness.windows import _click_label, _scroll_label

    assert _click_label("left", 1) == "click"
    assert _click_label("right", 1) == "right click"
    assert _click_label("left", 2) == "2x left click"
    assert _scroll_label(360, 0) == "scroll up dy=360 dx=0"
    assert _scroll_label(-120, 120) == "scroll down/right dy=-120 dx=120"


def test_zip_strict_matches_zip_strict_semantics():
    """zip_strict yields paired items and rejects unequal lengths on Py3.9+."""
    assert list(zip_strict([1, 2], ["a", "b"])) == [(1, "a"), (2, "b")]
    assert list(zip_strict([1, 2, 3], [4, 5, 6])) == [(1, 4), (2, 5), (3, 6)]
    with pytest.raises(ValueError):
        list(zip_strict([1, 2], ["a"]))
    with pytest.raises(ValueError):
        list(zip_strict([1], ["a", "b"]))


def test_text_landed_detects_dropped_chars():
    """_text_landed confirms append-at-end, flags shorter-than-typed drops, and
    leaves mid-document content ambiguous (None) rather than guessing."""
    typed = "hello world"
    # Fresh/replaced field: the typed text is at the end -> landed.
    assert _text_landed("prefix\nhello world", typed) is True
    assert _text_landed("hello world", typed) is True
    # A drop leaves the field shorter than what we typed -> not landed.
    assert _text_landed("hel", typed) is False
    # Text present mid-document (e.g. we typed into the middle) -> ambiguous.
    assert _text_landed("hello world then more", typed) is None
    # No read-back available -> nothing to compare.
    assert _text_landed(None, typed) is None
    # Empty typed text is trivially landed.
    assert _text_landed("anything", "") is True


def test_text_landed_with_before_snapshot():
    """With a before/after pair, _text_landed confirms mid-document inserts by
    presence-or-delta and flags a burst the document contradicts (the
    session-restore race from issue #1)."""
    typed = "hello world"
    before = "x" * 999
    # Typed into the middle of existing content: present now, absent before.
    assert _text_landed("x" * 500 + typed + "x" * 488, typed, before=before) is True
    # Typed at the end of existing content.
    assert _text_landed(before + typed, typed, before=before) is True
    # Payload already present elsewhere: confirmed by insertion delta.
    already = typed + "x" * 999
    assert _text_landed(typed + "x" * 500 + typed + "x" * 499, typed, before=already) is True
    # The restore race: the doc grew by only a mangled fragment of the burst.
    assert _text_landed(before + "hel", typed, before=before) is False
    # Nothing landed at all.
    assert _text_landed(before, typed, before=before) is False
    # CRLF payload vs CRLF document: delta compares on normalized newlines.
    multiline = "line one\r\nline two"
    assert _text_landed(before + "prefix\r\nline one\r\nline two", multiline, before=before + "prefix") is True


def test_normalize_newlines_collapses_crlf():
    assert _normalize_newlines("a\r\nb\rc") == "a\nb\nc"
    assert _normalize_newlines("plain") == "plain"


@pytest.mark.skipif(not is_interactive_desktop(), reason="no interactive desktop")
def test_virtual_screen_bounds_positive():
    """The virtual desktop bounds are positive on an interactive desktop."""
    left, top, width, height = virtual_screen_bounds()
    assert width > 0
    assert height > 0


@pytest.mark.skipif(not is_interactive_desktop(), reason="no interactive desktop")
def test_capture_screen_returns_whole_image():
    """capture_screen returns a full-image dict with virtual-desktop bounds."""
    shot = capture_screen()
    assert shot["image"].width > 0
    assert shot["image"].height > 0
    assert shot["hwnd"] == 0
    assert shot["client_bounds"]["width"] == shot["image"].width


def test_capture_shot_bring_to_front_holds_visible_foreground(monkeypatch):
    """see(bring_to_front=True) fronts the target (cloak=False, hold=True)
    before recapturing, so the window lands and stays in front."""
    from contextlib import contextmanager

    import windows_harness.windows as win_mod

    calls: list = []
    shot = {"image": object(), "minimized": False, "backend": "printwindow"}
    monkeypatch.setattr(
        win_mod.Windows, "_resolve_hwnd",
        lambda self, app: (0x111, {"hwnd": 0x111, "pid": 1, "process": "x", "title": "x"}),
    )
    monkeypatch.setattr(
        win_mod, "capture_window",
        lambda h: calls.append("capture") or shot,
    )

    @contextmanager
    def fake_focus(hwnd, *, cloak=True, hold=False):
        calls.append(("focus", hwnd, cloak, hold))
        yield True

    monkeypatch.setattr(win_mod.inject, "cloaked_focus", fake_focus)

    hwnd, _info, _out = win_mod.Windows()._capture_shot("Notepad", bring_to_front=True)
    assert hwnd == 0x111
    assert ("focus", 0x111, False, True) in calls  # visible, held until release
    assert calls.count("capture") == 2  # grabbed once, recaptured while fronted


def test_capture_shot_default_stays_background(monkeypatch):
    """The default capture never fronts; a normal (non-minimized) window is
    captured once with no cloaked-focus round at all."""
    import windows_harness.windows as win_mod

    calls: list = []
    shot = {"image": object(), "minimized": False, "backend": "printwindow"}
    monkeypatch.setattr(
        win_mod.Windows, "_resolve_hwnd",
        lambda self, app: (0x111, {"hwnd": 0x111}),
    )
    monkeypatch.setattr(
        win_mod, "capture_window",
        lambda h: calls.append("capture") or shot,
    )
    monkeypatch.setattr(
        win_mod.inject, "cloaked_focus",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not front by default")),
    )

    win_mod.Windows()._capture_shot("Notepad")
    assert calls == ["capture"]


def test_capture_shot_minimized_default_still_cloaks(monkeypatch):
    """The minimized-window default path is unchanged: restore under the
    cloak, capture, then re-minimize (cloak=True, hold=False)."""
    from contextlib import contextmanager

    import windows_harness.windows as win_mod

    calls: list = []
    shot = {"image": object(), "minimized": True, "backend": "printwindow"}
    monkeypatch.setattr(
        win_mod.Windows, "_resolve_hwnd",
        lambda self, app: (0x111, {"hwnd": 0x111}),
    )
    monkeypatch.setattr(
        win_mod, "capture_window",
        lambda h: calls.append("capture") or shot,
    )

    @contextmanager
    def fake_focus(hwnd, *, cloak=True, hold=False):
        calls.append(("focus", hwnd, cloak, hold))
        yield True

    monkeypatch.setattr(win_mod.inject, "cloaked_focus", fake_focus)

    win_mod.Windows()._capture_shot("Notepad")
    assert ("focus", 0x111, True, False) in calls  # invisible restore, no hold
    assert calls.count("capture") == 2


def test_cli_see_exposes_bring_to_front():
    """`windows-harness see --bring-to-front` is wired through."""
    from windows_harness import cli

    parser = cli._build_parser()
    args = parser.parse_args(["see", "Notepad", "--bring-to-front"])
    assert args.bring_to_front is True
    plain = parser.parse_args(["see", "Notepad"])
    assert plain.bring_to_front is False
