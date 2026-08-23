"""Proof-of-action markers stamped onto captured screenshots.

After a coordinate action (click / drag / scroll / hover / click-to-type) the
harness redraws the intended point onto the screenshot the coordinates were
anchored to and hands back the path under ``result["proof"]["path"]``. The
agent opens it only when it needs to confirm WHERE it acted (a mis-click, a
no-op, a wrong target), not on every call — see SKILL.md.

The drawing helpers are pure (no GUI, no Win32): they take a PIL image or draw
of an arbitrary canvas, so they can be unit-tested off a desktop and reused
for any screenshot, resized or native.
"""

from __future__ import annotations

import math
from typing import Any

from PIL import Image, ImageDraw

# Hard marker colours with a black drop shadow so they read on both light and
# dark UI without depending on the screenshot's own content.
_RED = (255, 45, 45)
_ORANGE = (255, 140, 0)
_BLUE = (60, 140, 255)
_SHADOW = (0, 0, 0)

_RING_RADIUS = 14.0
_CROSS_HALF = 10.0
_CENTER_RADIUS = 2.5
_STROKE = 3
_ARROW_HEAD = 11.0
_ARROW_MIN = 34.0
_ARROW_MAX = 96.0
_DRAG_MAX = 160.0
_DOT_RADIUS = 3.5

_FONT: Any = None


def _font() -> Any:
    """A small readable TrueType font with a safe fallback."""
    global _FONT
    if _FONT is not None:
        return _FONT
    from PIL import ImageFont

    for name in ("arial.ttf", "segoeui.ttf", "tahoma.ttf"):
        try:
            _FONT = ImageFont.truetype(name, 13)
            return _FONT
        except OSError:
            continue
    try:
        _FONT = ImageFont.load_default(size=13)
    except Exception:  # noqa: BLE001 - old PIL has no size kwarg
        _FONT = ImageFont.load_default()
    return _FONT


def _reticle(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    color: tuple[int, int, int],
    radius: float,
    cross: float,
    center: float,
    width: int,
) -> None:
    """One circle-cross (ring + crosshair + centre dot) in a single colour."""
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline=color, width=width)
    draw.line((x - cross, y, x + cross, y), fill=color, width=width)
    draw.line((x, y - cross, x, y + cross), fill=color, width=width)
    draw.ellipse((x - center, y - center, x + center, y + center), fill=color)


def draw_click(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    *,
    color: tuple[int, int, int] = _RED,
) -> None:
    """A click/press reticle: red circle-cross, black offset shadow for depth."""
    x, y = float(x), float(y)
    _reticle(draw, x + 2, y + 2, _SHADOW, _RING_RADIUS + 1, _CROSS_HALF + 1, _CENTER_RADIUS + 1, _STROKE + 1)
    _reticle(draw, x, y, color, _RING_RADIUS, _CROSS_HALF, _CENTER_RADIUS, _STROKE)


def _arrow(
    draw: ImageDraw.ImageDraw,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    color: tuple[int, int, int],
    *,
    width: int = _STROKE,
) -> None:
    """A line from (x0,y0) to (x1,y1) with an arrowhead and a start dot."""
    draw.line((x0, y0, x1, y1), fill=color, width=width)
    angle = math.atan2(y1 - y0, x1 - x0)
    for spread in (math.radians(150), math.radians(-150)):
        hx = x1 + math.cos(angle + spread) * _ARROW_HEAD
        hy = y1 + math.sin(angle + spread) * _ARROW_HEAD
        draw.line((x1, y1, hx, hy), fill=color, width=width)
    draw.ellipse(
        (x0 - _DOT_RADIUS, y0 - _DOT_RADIUS, x0 + _DOT_RADIUS, y0 + _DOT_RADIUS),
        fill=color,
    )


def draw_scroll(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    delta_y: int,
    delta_x: int,
    *,
    color: tuple[int, int, int] = _RED,
) -> None:
    """A wheel-scroll arrow starting at the anchor point.

    Image space grows downward, so a positive ``delta_y`` (scroll up) points
    up, and a positive ``delta_x`` (tilt right) points right. The arrow length
    tracks the notch count, clamped to a readable size.
    """
    x, y = float(x), float(y)
    dx = float(delta_x or 0)
    dy = -(float(delta_y or 0))
    if dx == 0 and dy == 0:
        dy = -1.0
    notches = (abs(float(delta_y or 0)) + abs(float(delta_x or 0))) / 120.0
    length = min(_ARROW_MAX, max(_ARROW_MIN, _ARROW_MIN + notches * 6))
    norm = math.hypot(dx, dy) or 1.0
    tx, ty = x + dx / norm * length, y + dy / norm * length
    _arrow(draw, x + 2, y + 2, tx + 2, ty + 2, _SHADOW)
    _arrow(draw, x, y, tx, ty, color)
    _reticle(draw, x + 2, y + 2, _SHADOW, 7, 5, 1.5, _STROKE + 1)
    _reticle(draw, x, y, color, 7, 5, 1.5, _STROKE)


def draw_drag(
    draw: ImageDraw.ImageDraw,
    sx: float,
    sy: float,
    ex: float,
    ey: float,
    *,
    color: tuple[int, int, int] = _BLUE,
) -> None:
    """A drag glide: reticle at the start, line + arrowhead, square at the end."""
    sx, sy, ex, ey = float(sx), float(sy), float(ex), float(ey)
    dx, dy = ex - sx, ey - sy
    length = math.hypot(dx, dy) or 1.0
    scale = min(1.0, _DRAG_MAX / length) if length > _DRAG_MAX else 1.0
    tx, ty = sx + dx * scale, sy + dy * scale
    _arrow(draw, sx + 2, sy + 2, tx + 2, ty + 2, _SHADOW)
    _arrow(draw, sx, sy, tx, ty, color)
    _reticle(draw, sx + 2, sy + 2, _SHADOW, 9, 6, 2.5, _STROKE + 1)
    _reticle(draw, sx, sy, color, 9, 6, 2.5, _STROKE)
    half = 5.0
    draw.rectangle((tx - half, ty - half, tx + half, ty + half), outline=color, width=_STROKE)


def _label(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    text: str,
    color: tuple[int, int, int],
) -> None:
    """Draw the action name just up-and-right of the marker, with a shadow."""
    lx, ly = float(x) + 18, float(y) - 24
    try:
        font = _font()
        draw.text((lx + 1, ly + 1), text, fill=_SHADOW, font=font)
        draw.text((lx, ly), text, fill=color, font=font)
    except Exception:  # noqa: BLE001 - never let a label break the proof
        pass


def annotate(image: Image.Image, actions: list[dict[str, Any]]) -> Image.Image:
    """Stamp a list of markers onto an image (in place) and return it.

    Each ``action`` is a dict with ``kind`` plus geometry:

    - ``click``/``hover``/``focus``: ``x``, ``y``
    - ``scroll``: ``x``, ``y``, ``delta_x``, ``delta_y``
    - ``drag``: ``x``, ``y``, ``end_x``, ``end_y``

    All kinds accept an optional ``label`` drawn beside the marker.
    """
    draw = ImageDraw.Draw(image)
    for action in actions:
        kind = action.get("kind", "click")
        x, y = float(action["x"]), float(action["y"])
        label = str(action.get("label", ""))
        if kind == "scroll":
            draw_scroll(
                draw, x, y,
                int(action.get("delta_y", 0)), int(action.get("delta_x", 0)),
            )
            label_color = _RED
        elif kind == "drag":
            draw_drag(draw, x, y, float(action.get("end_x", x)), float(action.get("end_y", y)))
            label_color = _BLUE
        elif kind == "hover":
            draw_click(draw, x, y, color=_ORANGE)
            label_color = _ORANGE
        else:  # click / focus / press
            draw_click(draw, x, y)
            label_color = _RED
        if label:
            _label(draw, x, y, label, label_color)
    return image
