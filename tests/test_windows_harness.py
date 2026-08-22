"""Smoke tests. GUI-dependent tests skip without an interactive desktop."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from windows_harness.capture import (  # noqa: E402
    HarnessError,
    capture_window,
    enumerate_windows,
    is_interactive_desktop,
    resolve_hwnd,
)
from windows_harness.delivery import (  # noqa: E402
    post_message_blocked_by_uipi,
)
from windows_harness.inject import (  # noqa: E402
    _split_scroll_delta,
    _utf16_code_units,
    parse_combo,
    pack_lparam,
    vk_for_key,
)
from windows_harness.pointer import POINTER_HOTSPOT, pointer_points  # noqa: E402


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
    from windows_harness import delivery, windows as windows_module

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
    monkeypatch.setattr(
        windows_module, "client_to_screen",
        lambda hwnd, x, y: (100 + x, 200 + y),
    )
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
