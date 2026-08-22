"""The explicit UI Automation escape hatch for the Windows harness."""

from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING, Any, Iterable

from . import inject
from .capture import HarnessError

if TYPE_CHECKING:
    from .windows import Windows

# Bound lazily by _require_uia(): importing uiautomation up front would tax
# every CLI call (see/apps/exec/run) that never touches the UIA tree.
auto: Any = None


_COMPACT_ATTRIBUTES = (
    "ControlTypeName",
    "Name",
    "AutomationId",
    "ClassName",
    "Value",
    "BoundingRectangle",
)

_ACTION_ALIASES = {
    "press": "invoke",
    "click": "invoke",
    "toggle": "toggle",
    "expand": "expand",
    "collapse": "collapse",
    "select": "select",
    "focus": "setfocus",
}

_PATTERN_METHODS = {
    "invoke": ("GetInvokePattern", "Invoke"),
    "toggle": ("GetTogglePattern", "Toggle"),
    "expand": ("GetExpandCollapsePattern", "Expand"),
    "collapse": ("GetExpandCollapsePattern", "Collapse"),
    "select": ("GetSelectionItemPattern", "Select"),
    "scroll": ("GetScrollPattern", "ScrollIntoView"),
    "setfocus": (None, "SetFocus"),
    "default": ("GetLegacyIAccessiblePattern", "DoDefaultAction"),
}

# CEF/Electron windows (class Chrome_WidgetWin*) fill their accessibility
# tree lazily — only after the window has held the foreground at least once
# (VS Code measured: 19 nodes cold, 1350 after one focus round). A CEF tree
# with fewer live nodes than this is almost certainly cold, not small.
_CEF_CLASS_PREFIX = "chrome_widgetwin"
_COLD_TREE_NODE_LIMIT = 32


def _require_uia() -> None:
    global auto
    if auto is None:
        try:
            import uiautomation as uia
        except ImportError as exc:  # pragma: no cover - exercised when deps are missing
            raise HarnessError(
                "The uiautomation package is unavailable. Install project "
                "dependencies first: `uv sync` or `pip install -e .`"
            ) from exc
        auto = uia


class Accessibility:
    """Thin, opt-in access to Windows UI Automation."""

    def __init__(self, host: Windows) -> None:
        self._host = host

    # --- discovery -------------------------------------------------------

    def dump(
        self,
        app: str,
        *,
        max_depth: int = 12,
        max_nodes: int = 1500,
        screenshot: bool = False,
    ) -> dict[str, Any]:
        """Snapshot one app's UIA tree as compact indented text.

        Frames are reported in the pixel space of the latest screenshot of
        this window (``frame_space: "screenshot"``) so they line up with what
        the model sees; without a matching screenshot they stay physical
        screen pixels (``frame_space: "screen"``, key ``frame_screen``).
        """
        _require_uia()
        hwnd, _info = self._host._resolve_hwnd(app)
        # Shoot first: frames then map into THIS screenshot's space.
        shot = self._host.see(app) if screenshot else None
        root, cold = self._root_from_hwnd(hwnd)
        frame_map = self._frame_mapper(hwnd)
        with self._walk_context(hwnd):
            nodes = self._snapshot_tree(
                root, max_depth=max_depth, max_nodes=max_nodes,
                frame_map=frame_map,
            )
        state: dict[str, Any] = {
            "hwnd": hwnd,
            "frame_space": "screenshot" if frame_map is not None else "screen",
            "nodes": nodes,
            "text": self._render_tree(nodes, truncated=len(nodes) >= max_nodes),
        }
        if cold:
            state["note"] = (
                "CEF tree stayed near-empty after a focus warm-up; this app "
                "may not expose UIA (launch it with "
                "--force-renderer-accessibility) — use see() + coordinates"
            )
        if shot is not None:
            state["screenshot"] = shot
        return state

    def query(
        self,
        *,
        app: str | None = None,
        control_type: str | None = None,
        name: str | None = None,
        value: str | None = None,
        limit: int = 20,
        max_nodes: int = 2000,
    ) -> list[dict[str, Any]]:
        """Search the tree for controls by type, name, or value substrings.

        ``control_type`` matches with or without the "Control" suffix:
        "Button" and "ButtonControl" are the same query.
        """
        _require_uia()
        root, hwnd, _cold = self._root_for(app)
        frame_map = self._frame_mapper(hwnd)
        wanted_type = _norm_control_type(control_type) if control_type else None
        needle_name = name.casefold() if name else None
        needle_value = value.casefold() if value else None

        matches: list[dict[str, Any]] = []
        visited = 0
        stack = [root]
        with self._walk_context(hwnd):
            while stack and visited < max_nodes and len(matches) < limit:
                element = stack.pop()
                visited += 1
                try:
                    # Push children BEFORE filtering: a control that fails an
                    # attribute read (e.g. window roots have no .Value) must
                    # not take its whole subtree down with it.
                    stack.extend(element.GetChildren())
                except Exception:  # noqa: BLE001 - dead COM elements drop out
                    continue
                try:
                    if wanted_type and _norm_control_type(element.ControlTypeName) != wanted_type:
                        continue
                    if needle_name and needle_name not in (element.Name or "").casefold():
                        continue
                    if needle_value and needle_value not in str(
                        getattr(element, "Value", None) or ""
                    ).casefold():
                        continue
                except Exception:  # noqa: BLE001 - unreadable attributes skip one node
                    continue
                index = self._host._remember_element(element)
                matches.append(
                    self._describe(
                        element, index, attributes=_COMPACT_ATTRIBUTES,
                        frame_map=frame_map,
                    )
                )
        return matches

    def at(
        self,
        x: float,
        y: float,
        *,
        app: str | None = None,
        coordinate_space: str = "screen",
    ) -> dict[str, Any]:
        """Describe the UIA element under a point, searched within one app."""
        _require_uia()
        point = self._host._screen_point(x, y, coordinate_space)
        if app is None:
            element = auto.ControlFromPoint(*point)
            if element is None:
                raise HarnessError(f"No UIA element at screen point {point}")
            index = self._host._remember_element(element)
            return self._describe(element, index, attributes=_COMPACT_ATTRIBUTES)
        hwnd, _info = self._host._resolve_hwnd(app)
        root, _cold = self._root_from_hwnd(hwnd)
        with self._walk_context(hwnd):
            element = self._deepest_at_point(root, point)
        if element is None:
            raise HarnessError(f"No UIA element of {app!r} at screen point {point}")
        index = self._host._remember_element(element)
        return self._describe(
            element, index, attributes=_COMPACT_ATTRIBUTES,
            frame_map=self._frame_mapper(hwnd),
        )

    # --- element operations ----------------------------------------------

    def get(self, element_index: int, attribute: str = "Name") -> Any:
        def read(element: Any) -> Any:
            try:
                return getattr(element, attribute)
            except AttributeError:
                if attribute != "Value":
                    raise
                # The library only materializes .Value on some control types;
                # the patterns carry it for the rest (editors, documents).
                for getter in ("GetValuePattern", "GetLegacyIAccessiblePattern"):
                    pattern = getattr(element, getter)()
                    if pattern is not None and pattern.Value is not None:
                        return pattern.Value
                raise

        return self._call(
            element_index, f"read {attribute}", read, unknown_attribute=attribute,
        )

    def set_value(self, element_index: int, value: str) -> None:
        def write(element: Any) -> None:
            pattern = element.GetValuePattern()
            if pattern is None:
                raise HarnessError(
                    f"Element {element_index} exposes no ValuePattern; "
                    "use win.type() instead"
                )
            text = str(value)
            pattern.SetValue(text)
            # Verify-after-write: SetValue can be accepted and silently ignored.
            if str(pattern.Value or "") != text:
                raise HarnessError(
                    f"SetValue on element {element_index} did not take effect"
                )

        self._call(element_index, "set_value", write)

    def actions(self, element_index: int) -> list[str]:
        def probe(element: Any) -> list[str]:
            available: list[str] = []
            for action, (getter, _method) in _PATTERN_METHODS.items():
                if getter is None:
                    available.append(action)
                    continue
                try:
                    if getattr(element, getter)() is not None:
                        available.append(action)
                except Exception:  # noqa: BLE001
                    continue
            return available

        return self._call(element_index, "list actions", probe)

    def perform(self, element_index: int, action: str = "invoke") -> None:
        def act(element: Any) -> None:
            normalized = _ACTION_ALIASES.get(action.casefold(), action.casefold())
            try:
                getter, method = _PATTERN_METHODS[normalized]
            except KeyError as exc:
                raise HarnessError(
                    f"Unknown action {action!r}; available: {sorted(_PATTERN_METHODS)}"
                ) from exc
            target = element if getter is None else getattr(element, getter)()
            if getter is not None and target is None:
                raise HarnessError(
                    f"Element {element_index} does not expose {normalized!r}; "
                    f"available: {self.actions(element_index)}"
                )
            getattr(target, method)()

        self._call(element_index, f"perform {action!r}", act)

    def raw(self, element_index: int) -> Any:
        return self._element(element_index)

    # --- element-centered input --------------------------------------------

    def click(
        self,
        element_index: int,
        *,
        app: str | None = None,
        button: str = "left",
        clicks: int = 1,
        delivery: str = "foreground",
        hold: bool = True,
    ) -> dict[str, Any]:
        """Click the center of an element's BoundingRectangle.

        Coordinates never leave the harness, so no coordinate-space mismatch
        is possible; prefer this over copying frame values into win.click().
        """
        if delivery == "foreground":
            hwnd, _info = self._host._resolve_hwnd(app)
            if inject.user32.IsIconic(hwnd):
                # A minimized window's BoundingRectangles are the parked
                # iconic rect — restore before reading element geometry.
                with inject.cloaked_focus(hwnd, cloak=False, hold=hold):
                    cx, cy = self._element_center(element_index)
                    return self._host.click(
                        cx, cy, app=app, button=button, clicks=clicks,
                        coordinate_space="screen", delivery=delivery, hold=hold,
                    )
        cx, cy = self._element_center(element_index)
        return self._host.click(
            cx, cy, app=app, button=button, clicks=clicks,
            coordinate_space="screen", delivery=delivery, hold=hold,
        )

    def hover(
        self,
        element_index: int,
        *,
        app: str | None = None,
        dwell: float = 0.6,
        delivery: str = "foreground",
        hold: bool = True,
    ) -> dict[str, Any]:
        """Really hover the element's center so its tooltip/state fires —
        the way to read names of icon-only controls (server/avatar images).

        Unlike ``win.hover`` (coordinate-routed, never fronts), an
        element-targeted hover carries interaction intent: a target occluded
        at the element's point is fronted first so the hover can land.
        """
        if delivery == "foreground":
            hwnd, _info = self._host._resolve_hwnd(app)
            if inject.user32.IsIconic(hwnd):
                # Same parked-rect trap as ax.click: restore first.
                with inject.cloaked_focus(hwnd, cloak=False, hold=hold):
                    cx, cy = self._element_center(element_index)
                    return self._host.hover(
                        cx, cy, app=app, dwell=dwell,
                        coordinate_space="screen", delivery=delivery, hold=hold,
                    )
            cx, cy = self._element_center(element_index)
            if not inject.target_visible_at_point(hwnd, int(cx), int(cy)):
                if hold:
                    with inject.cloaked_focus(hwnd, cloak=False, hold=True):
                        pass
                else:
                    with inject.cloaked_focus(hwnd, cloak=False, hold=False):
                        return self._host.hover(
                            cx, cy, app=app, dwell=dwell,
                            coordinate_space="screen", delivery=delivery,
                            hold=hold,
                        )
        else:
            cx, cy = self._element_center(element_index)
        return self._host.hover(
            cx, cy, app=app, dwell=dwell,
            coordinate_space="screen", delivery=delivery, hold=hold,
        )

    def _element_center(self, element_index: int) -> tuple[float, float]:
        element = self._element(element_index)
        try:
            rect = element.BoundingRectangle
        except Exception as exc:  # noqa: BLE001 - the window/tab died under us
            raise HarnessError(
                f"Element {element_index} died before its rectangle could be "
                "read (the window or tab changed); take a fresh "
                "ax.query()/ax.at()"
            ) from exc
        if rect is None or rect.isempty():
            raise HarnessError(
                f"Element {element_index} has no on-screen rectangle "
                "(offscreen or collapsed); scroll it into view first"
            )
        return (rect.left + rect.right) / 2.0, (rect.top + rect.bottom) / 2.0

    # --- internals ---------------------------------------------------------

    def _root_for(self, app: str | None) -> tuple[Any, int, bool]:
        if app is None and self._host._last_window is not None:
            app = str(self._host._last_window["hwnd"])
        if not app:
            raise HarnessError("Specify an app name, exe, title, or HWND")
        hwnd, _info = self._host._resolve_hwnd(app)
        root, cold = self._root_from_hwnd(hwnd)
        return root, hwnd, cold

    @staticmethod
    def _walk_context(hwnd: int) -> Any:
        """Geometry-readable context for a tree walk: a minimized window's
        BoundingRectangles are the parked iconic rect, so restore it
        (cloaked, invisible) for the walk and re-minimize afterwards."""
        if inject.user32.IsIconic(hwnd):
            return inject.cloaked_focus(hwnd, cloak=True, hold=False)
        return contextlib.nullcontext()

    def _root_from_hwnd(self, hwnd: int) -> tuple[Any, bool]:
        """UIA root for one window, warming cold CEF trees when needed.

        Returns ``(root, cold)`` — cold is True when a CEF window's tree
        stayed near-empty even after a focus warm-up round.
        """
        root = auto.ControlFromHandle(hwnd)
        if root is None:
            raise HarnessError(f"Window {hwnd:#x} exposes no UIA tree")
        if not self._is_cold_cef(root):
            return root, False
        # A brief, usually invisible (cloaked) foreground round flips
        # Chromium's accessibility tree on; re-read until it fills in.
        try:
            with inject.cloaked_focus(hwnd):
                for _ in range(4):
                    time.sleep(0.4)
                    root = auto.ControlFromHandle(hwnd)
                    if root is None or not self._is_cold_cef(root):
                        break
        except Exception:  # noqa: BLE001 - warm-up is best-effort
            pass
        if root is None:
            raise HarnessError(f"Window {hwnd:#x} exposes no UIA tree")
        return root, self._is_cold_cef(root)

    def _is_cold_cef(self, root: Any) -> bool:
        try:
            if not (root.ClassName or "").casefold().startswith(_CEF_CLASS_PREFIX):
                return False
        except Exception:  # noqa: BLE001
            return False
        return self._count_nodes(root, _COLD_TREE_NODE_LIMIT) < _COLD_TREE_NODE_LIMIT

    @staticmethod
    def _count_nodes(root: Any, budget: int) -> int:
        count = 0
        stack = [root]
        while stack and count < budget:
            element = stack.pop()
            count += 1
            try:
                stack.extend(element.GetChildren())
            except Exception:  # noqa: BLE001 - dead COM elements just drop out
                continue
        return count

    def _frame_mapper(self, hwnd: int) -> Any | None:
        """Map physical screen rects into the pixel space of the latest
        screenshot of THIS window, so ax frames line up with what the model
        sees in win.see() and work with the default coordinate space."""
        shot = self._host._last_screenshot
        if shot is None or shot["hwnd"] != hwnd:
            return None
        bounds = shot["client_bounds"]
        scale_x = float(shot["scale_x"])
        scale_y = float(shot["scale_y"])
        origin_x = float(bounds["x"])
        origin_y = float(bounds["y"])

        def convert(rect: Any) -> tuple[float, float, float, float]:
            return (
                (rect.left - origin_x) * scale_x,
                (rect.top - origin_y) * scale_y,
                rect.width() * scale_x,
                rect.height() * scale_y,
            )

        return convert

    def _element(self, element_index: int) -> Any:
        return self._host._element(element_index)

    def _call(
        self,
        element_index: int,
        operation: str,
        fn: Any,
        *,
        unknown_attribute: str | None = None,
    ) -> Any:
        """Run one UIA operation; a dead COM element becomes an honest error."""
        element = self._element(element_index)
        try:
            return fn(element)
        except HarnessError:
            raise
        except AttributeError as exc:
            if unknown_attribute is not None:
                raise HarnessError(
                    f"Unknown UIA attribute {unknown_attribute!r}"
                ) from exc
            raise
        except Exception as exc:  # noqa: BLE001 - the window/tab died under us
            raise HarnessError(
                f"Element {element_index} died during {operation} (the window "
                "or tab changed); take a fresh ax.query()/ax.at()"
            ) from exc

    def _describe(
        self, element: Any, element_index: int, *, attributes: Iterable[str],
        frame_map: Any | None = None,
    ) -> dict[str, Any]:
        node: dict[str, Any] = {"element_index": element_index}
        for attribute in attributes:
            try:
                value = getattr(element, attribute)
            except Exception:  # noqa: BLE001
                continue
            if attribute == "BoundingRectangle":
                if value is None or value.isempty():
                    continue
                if frame_map is not None:
                    x, y, w, h = frame_map(value)
                    node["frame"] = {
                        "x": round(x, 1),
                        "y": round(y, 1),
                        "width": round(w, 1),
                        "height": round(h, 1),
                    }
                else:
                    # No matching screenshot: keep physical screen pixels
                    # under a distinct key so they can't be fed to the
                    # default screenshot coordinate space by mistake.
                    node["frame_screen"] = {
                        "x": value.left,
                        "y": value.top,
                        "width": value.width(),
                        "height": value.height(),
                    }
                continue
            if value not in (None, ""):
                node[_snake(attribute)] = str(value)
        return node

    def _snapshot_tree(
        self, root: Any, *, max_depth: int, max_nodes: int,
        frame_map: Any | None = None,
    ) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []

        def visit(element: Any, depth: int) -> None:
            if depth > max_depth or len(nodes) >= max_nodes:
                return
            # Monotonic indices from the shared element table: handles handed
            # out before this dump can never alias a node of this dump — they
            # fail honestly instead of acting on the wrong control.
            index = self._host._remember_element(element)
            node = {"element_index": index, "depth": depth}
            for attribute in _COMPACT_ATTRIBUTES:
                try:
                    value = getattr(element, attribute)
                except Exception:  # noqa: BLE001
                    continue
                if attribute == "BoundingRectangle":
                    if value is not None and not value.isempty():
                        if frame_map is not None:
                            x, y, w, h = frame_map(value)
                            node["frame"] = f"({x:g},{y:g},{w:g},{h:g})"
                        else:
                            node["frame_screen"] = (
                                f"({value.left:g},{value.top:g},"
                                f"{value.width():g},{value.height():g})"
                            )
                    continue
                if value not in (None, ""):
                    node[_snake(attribute)] = str(value)
            nodes.append(node)
            try:
                children = element.GetChildren()
            except Exception:  # noqa: BLE001
                return
            for child in children:
                visit(child, depth + 1)

        visit(root, 0)
        return nodes

    @staticmethod
    def _render_tree(nodes: list[dict[str, Any]], *, truncated: bool = False) -> str:
        lines: list[str] = []
        for node in nodes:
            parts = [str(node["element_index"]), node.get("control_type_name") or "Control"]
            for key in ("name", "value", "automation_id", "class_name"):
                if key in node and node[key]:
                    encoded = json_dumps_short(str(node[key]))
                    parts.append(f"{key}={encoded}")
            for key in ("frame", "frame_screen"):
                if key in node:
                    parts.append(f"{key}={node[key]}")
            lines.append("  " * int(node["depth"]) + " ".join(parts))
        if truncated:
            lines.append("… tree truncated by max_nodes or max_depth")
        return "\n".join(lines)

    def _deepest_at_point(self, root: Any, point: tuple[int, int]) -> Any | None:
        best = None
        stack = [root]
        visited = 0
        while stack and visited < 600:
            element = stack.pop()
            visited += 1
            try:
                rect = element.BoundingRectangle
                if rect is None or rect.isempty():
                    continue
                if not rect.contains(int(point[0]), int(point[1])):
                    continue
                best = element
                stack.extend(element.GetChildren())
            except Exception:  # noqa: BLE001
                continue
        return best


def _snake(name: str) -> str:
    return "".join(
        ("_" + char.lower()) if char.isupper() else char for char in name
    ).lstrip("_")


def _norm_control_type(name: str) -> str:
    """Control-type comparison key: "Button" and "ButtonControl" are the same
    control type, so compare with the suffix stripped on both sides."""
    lowered = name.casefold()
    if lowered.endswith("control"):
        lowered = lowered[: -len("control")]
    return lowered


def json_dumps_short(value: str, limit: int = 120) -> str:
    import json

    value = value.replace("\n", "\\n")
    if len(value) > limit:
        value = value[: limit - 1] + "…"
    return json.dumps(value, ensure_ascii=False)
