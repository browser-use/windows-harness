"""The explicit UI Automation escape hatch for the Windows harness."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable

from .capture import HarnessError

try:
    import uiautomation as auto
except ImportError:  # pragma: no cover - exercised when deps are missing
    auto = None

if TYPE_CHECKING:
    from .windows import Windows


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


def _require_uia() -> None:
    if auto is None:
        raise HarnessError(
            "The uiautomation package is unavailable. Install project "
            "dependencies first: `uv sync` or `pip install -e .`"
        )


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
        """Snapshot one app's UIA tree as compact indented text."""
        _require_uia()
        hwnd, _info = self._host._resolve_hwnd(app)
        root = auto.ControlFromHandle(hwnd)
        if root is None:
            raise HarnessError(f"Window {hwnd:#x} exposes no UIA tree")
        nodes = self._snapshot_tree(root, max_depth=max_depth, max_nodes=max_nodes)
        state: dict[str, Any] = {
            "hwnd": hwnd,
            "nodes": nodes,
            "text": self._render_tree(nodes, truncated=len(nodes) >= max_nodes),
        }
        if screenshot:
            state["screenshot"] = self._host.see(app)
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
        """Search the tree for controls by type, name, or value substrings."""
        _require_uia()
        root = self._root_for(app)
        wanted_type = control_type.casefold() if control_type else None
        needle_name = name.casefold() if name else None
        needle_value = value.casefold() if value else None

        matches: list[dict[str, Any]] = []
        visited = 0
        stack = [root]
        while stack and visited < max_nodes and len(matches) < limit:
            element = stack.pop()
            visited += 1
            try:
                # Push children BEFORE filtering: a control that fails an
                # attribute read (e.g. window roots have no .Value) must not
                # take its whole subtree down with it.
                stack.extend(element.GetChildren())
            except Exception:  # noqa: BLE001 - dead COM elements just drop out
                continue
            try:
                if wanted_type and element.ControlTypeName.casefold() != wanted_type:
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
                self._describe(element, index, attributes=_COMPACT_ATTRIBUTES)
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
        root = auto.ControlFromHandle(hwnd)
        if root is None:
            raise HarnessError(f"Window {hwnd:#x} exposes no UIA tree")
        element = self._deepest_at_point(root, point)
        if element is None:
            raise HarnessError(f"No UIA element of {app!r} at screen point {point}")
        index = self._host._remember_element(element)
        return self._describe(element, index, attributes=_COMPACT_ATTRIBUTES)

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

    # --- internals ---------------------------------------------------------

    def _root_for(self, app: str | None) -> Any:
        if app is None and self._host._last_window is not None:
            app = str(self._host._last_window["hwnd"])
        if not app:
            raise HarnessError("Specify an app name, exe, title, or HWND")
        hwnd, _info = self._host._resolve_hwnd(app)
        root = auto.ControlFromHandle(hwnd)
        if root is None:
            raise HarnessError(f"Window {hwnd:#x} exposes no UIA tree")
        return root

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
        self, element: Any, element_index: int, *, attributes: Iterable[str]
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
                node["frame"] = {
                    "x": value.left,
                    "y": value.top,
                    "width": value.width(),
                    "height": value.height(),
                }
                continue
            if value not in (None, ""):
                node[_snake(attribute)] = str(value)
        return node

    def _snapshot_tree(self, root: Any, *, max_depth: int, max_nodes: int) -> list[dict[str, Any]]:
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
                        node["frame"] = (
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
            if "frame" in node:
                parts.append(f"frame={node['frame']}")
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


def json_dumps_short(value: str, limit: int = 120) -> str:
    import json

    value = value.replace("\n", "\\n")
    if len(value) > limit:
        value = value[: limit - 1] + "…"
    return json.dumps(value, ensure_ascii=False)
