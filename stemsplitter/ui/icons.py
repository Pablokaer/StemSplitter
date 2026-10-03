"""One family of outline icons (24×24 grid, 2 px round strokes), drawn from inline SVG with QtSvg.

No icon files or icon-font dependency: `icon()` and `pixmap()` render a shape in any color.
"""

from __future__ import annotations

import math
from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer


def _gear() -> str:
    """A gear outline: 8 teeth around a ring, computed so it is exactly symmetric."""
    teeth, r_out, r_in = 8, 10.0, 7.6
    points = []
    for i in range(teeth):
        a = 2 * math.pi * i / teeth
        for da, r in ((-0.17, r_in), (-0.11, r_out), (0.11, r_out), (0.17, r_in)):
            points.append((12 + r * math.cos(a + da), 12 + r * math.sin(a + da)))
    d = "M" + " L".join(f"{x:.2f} {y:.2f}" for x, y in points) + " Z"
    return f'<path d="{d}"/><circle cx="12" cy="12" r="3"/>'


_SHAPES = {
    "waveform": '<path d="M2 12h2M6 8v8M10 4v16M14 7v10M18 10v4M22 12h-2"/>',
    "layers": '<path d="M12 3 2 8l10 5 10-5z"/><path d="m2 13 10 5 10-5"/><path d="m2 17.5 10 5 10-5" opacity="0.6"/>',
    "settings": _gear(),
    "info": '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>',
    "upload": '<path d="M7 18a5 5 0 0 1-.9-9.9A6 6 0 0 1 17.7 8.1 4.5 4.5 0 0 1 17 17h-1"/>'
              '<path d="M12 21v-9M8.5 15.5 12 12l3.5 3.5"/>',
    "file": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/>'
            '<path d="M12 18v-6M9 15h6"/>',
    "folder": '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.7-.9L9.6 3.9A2 2 0 0 0 7.9 3H4'
              'a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2z"/>',
    "trash": '<path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2'
             'M10 11v6M14 11v6"/>',
    "close": '<path d="M18 6 6 18M6 6l12 12"/>',
    "chip": '<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6" rx="1"/>'
            '<path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
    "music": '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "check-bold": '<path d="M20 6 9 17l-5-5" stroke-width="3.2"/>',
    "alert": '<circle cx="12" cy="12" r="10"/><path d="M12 7.5v5M12 16.5h.01"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><path d="M12 6.5V12l3.5 2"/>',
    "stop": '<circle cx="12" cy="12" r="10"/><rect x="9" y="9" width="6" height="6" rx="1"/>',
    "log": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/>'
           '<path d="M16 13H8M16 17H8M10 9H8"/>',
    "folder-open": '<path d="m6 14 1.5-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.54 6a2 2 0 0 1-1.95 1.5H4'
                   'a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.81 1.2a2 2 0 0 0 1.67.9H18a2 2 0 0 1 2 2v2"/>',
    "memory": '<rect x="2" y="7" width="20" height="10" rx="2"/><path d="M6 11v2M10 11v2M14 11v2M18 11v2'
              'M6 17v3M18 17v3"/>',
    "chevron-down": '<path d="m6 9 6 6 6-6"/>',
    "arrow-left": '<path d="M19 12H5M12 19l-7-7 7-7"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
    "globe": '<circle cx="12" cy="12" r="10"/><path d="M2 12h20"/>'
             '<path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>',
    "refresh": '<path d="M21 12a9 9 0 1 1-2.64-6.36L21 8"/><path d="M21 3v5h-5"/>',
}


def _svg(name: str, color: str) -> bytes:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
            f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{_SHAPES[name]}</svg>').encode()


def _screen_ratio() -> float:
    screen = QGuiApplication.primaryScreen()
    return max(2.0, screen.devicePixelRatio() if screen else 1.0)


def _render(svg: bytes, size: int, ratio: float) -> QPixmap:
    px = max(1, round(size * ratio))
    pm = QPixmap(px, px)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    QSvgRenderer(QByteArray(svg)).render(painter, QRectF(0, 0, px, px))
    painter.end()
    pm.setDevicePixelRatio(ratio)
    return pm


@lru_cache(maxsize=256)
def pixmap(name: str, color: str, size: int, ratio: float = 0) -> QPixmap:
    """`size` in device-independent pixels; drawn at the screen's pixel ratio (at least 2× for crispness)."""
    return _render(_svg(name, color), size, ratio or _screen_ratio())


def icon(name: str, color: str, size: int = 20, checked_color: str | None = None,
         disabled_color: str | None = None) -> QIcon:
    """A QIcon; `checked_color` is used when a checkable button is on, `disabled_color` when disabled."""
    ic = QIcon()
    ic.addPixmap(pixmap(name, color, size), QIcon.Normal, QIcon.Off)
    if checked_color:
        ic.addPixmap(pixmap(name, checked_color, size), QIcon.Normal, QIcon.On)
    if disabled_color:
        ic.addPixmap(pixmap(name, disabled_color, size), QIcon.Disabled, QIcon.Off)
    return ic


def logo(size: int = 44) -> QPixmap:
    """The app mark: a rounded square with a purple gradient and a waveform."""
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 44 44">'
           '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
           '<stop offset="0" stop-color="#9B7BFF"/><stop offset="1" stop-color="#5B3DF0"/></linearGradient></defs>'
           '<rect width="44" height="44" rx="12" fill="url(#g)"/>'
           '<g stroke="#FFFFFF" stroke-width="3" stroke-linecap="round">'
           '<path d="M11 19v6M16.5 14v16M22 10v24M27.5 15v14M33 19v6"/></g></svg>').encode()
    return _render(svg, size, _screen_ratio())
