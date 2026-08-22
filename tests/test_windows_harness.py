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
