"""Design tokens (colors, type, spacing, radii) and the application style sheet.

Every color and size the window uses is defined here, so the look can be changed in one place.
"""

from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path

from PySide6.QtGui import QColor, QFontDatabase, QPalette


class Colors:
    """The OctoSplitter palette: near-black surfaces, one red for identity, feedback and selection."""

    BACKGROUND = "#080808"
    SURFACE = "#101010"  # sidebar, status bar, drop zone
    SURFACE_2 = "#141414"  # cards
    SURFACE_3 = "#191919"  # fields, rows
    ELEVATED = "#1E1E1E"  # hover, popups, buttons
    PRIMARY = "#FF1838"
    PRIMARY_HOVER = "#FF3A55"
    PRIMARY_PRESSED = "#E5092F"
    PRIMARY_DARK = "#8F0D24"
    PRIMARY_LIGHT = "#FF4A5F"  # highlights, icons on dark
    PRIMARY_TINT = "#2A0A10"  # selected row / drag-over background
    WAVE_ACTIVE = "#FF1838"  # waveform: the part already played
    WAVE_IDLE = "#D81232"  # waveform: the part still to play
    WAVE_DIM = "#59101D"  # waveform: a muted stem
    TEXT = "#F5F5F5"
    TEXT_2 = "#A7A7A7"
    TEXT_MUTED = "#707070"
    TEXT_DISABLED = "#4A4A4A"
    BORDER = "rgba(255,255,255,0.08)"
    BORDER_SOFT = "rgba(255,255,255,0.06)"
    BORDER_HOVER = "rgba(255,255,255,0.16)"
    BORDER_ACCENT = "rgba(255,24,56,0.50)"
    TRACK = "#242424"  # empty part of progress bars and sliders
    SUCCESS = "#3DDC84"
    DANGER = "#FF7A85"  # failures: lighter than the brand red so the two never read as the same thing
    GRADIENT = "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #FF354D, stop:1 #D4002C)"
    GRADIENT_HOVER = "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #FF5468, stop:1 #E5092F)"


def rgba(hex_color: str, alpha: float) -> str:
    """`#RRGGBB` + opacity -> a color string Qt style sheets understand."""
    h = hex_color.lstrip("#")
    return f"rgba({int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)},{alpha})"


class Spacing:
    XS = 4
    SM = 8
    MD = 12
    LG = 16
    XL = 24
    XXL = 32
    XXXL = 48


class Radii:
    INPUT = 10
    BUTTON = 10
    NAV = 12
    CARD = 16
    DROP = 20


class Type:
    """Pixel sizes; the family is picked at startup by `pick_font_family()`."""

    PAGE_TITLE = 28
    APP_TITLE = 22
    SECTION = 17
    BODY = 14
    SECONDARY = 13
    SMALL = 12
    family = "Segoe UI"


class Sizes:
    WINDOW_OPEN = (1180, 760)  # the size the window opens at
    WINDOW_MIN = (900, 640)
    SIDEBAR = 248
    SIDEBAR_COMPACT = 76  # icons only, when the window is narrow
    COMPACT_BELOW = 1000  # window width under which the sidebar collapses
    CONTROL = 44  # line edits and combo boxes
    BUTTON = 44
    BUTTON_PRIMARY = 48
    BUTTON_SMALL = 36
    ROW = 68  # one file in the queue
    STEM_ROW = 64  # one stem channel
    DROP_ZONE = 216
    PROGRESS = 8


PREFERRED_FONTS = ["Inter", "Inter Variable", "Segoe UI Variable Text", "Segoe UI", "SF Pro Text",
                   ".AppleSystemUIFont", "Helvetica Neue", "Arial"]


# Scripts the Latin fonts above don't cover. Left to Qt's per-glyph fallback, Devanagari lost its vowel signs
# in bold text and its lines overlapped (Windows 11), so these languages use a font made for their script.
SCRIPT_FONTS = {
    "hi": ["Nirmala UI", "Kohinoor Devanagari", "Devanagari Sangam MN", "Noto Sans Devanagari", "Mangal"],
    "zh": ["Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC"],
}


def pick_font_family(language: str = "en") -> str:
    families = set(QFontDatabase.families())
    for name in SCRIPT_FONTS.get(language, []) + PREFERRED_FONTS:
        if name in families:
            Type.family = name
            break
    return Type.family


def dark_palette() -> QPalette:
    """For the few things the style sheet doesn't reach (Fusion-drawn frames, some dialogs)."""
    p = QPalette()
    roles = {
        QPalette.Window: Colors.BACKGROUND, QPalette.WindowText: Colors.TEXT, QPalette.Base: Colors.SURFACE_3,
        QPalette.AlternateBase: Colors.SURFACE_2, QPalette.Text: Colors.TEXT, QPalette.Button: Colors.ELEVATED,
        QPalette.ButtonText: Colors.TEXT, QPalette.Highlight: Colors.PRIMARY, QPalette.HighlightedText: "#FFFFFF",
        QPalette.ToolTipBase: Colors.ELEVATED, QPalette.ToolTipText: Colors.TEXT,
        QPalette.PlaceholderText: Colors.TEXT_MUTED, QPalette.Mid: "#2A2A2A", QPalette.Midlight: "#1C1C1C",
        QPalette.Link: Colors.PRIMARY_LIGHT,
    }
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor(Colors.TEXT_DISABLED))
    return p


def _write_style_images() -> Path:
    """The style sheet needs image files for the combo arrow and the check mark: draw them once per run."""
    from .icons import pixmap

    folder = Path(tempfile.mkdtemp(prefix="octosplitter-ui-"))
    atexit.register(shutil.rmtree, folder, True)
    for name, icon, color, size in [("chevron", "chevron-down", Colors.TEXT_2, 16),
                                    ("chevron-disabled", "chevron-down", Colors.TEXT_DISABLED, 16),
                                    ("check", "check-bold", "#FFFFFF", 14)]:
        for scale, suffix in ((1, ""), (2, "@2x"), (3, "@3x")):
            pm = pixmap(icon, color, size, ratio=scale).copy()  # a copy: the cached one keeps its ratio
            pm.setDevicePixelRatio(1)
            pm.save(str(folder / f"{name}{suffix}.png"))
    return folder


def stylesheet() -> str:
    img = _write_style_images().as_posix()
    c, r, t = Colors, Radii, Type
    return f"""
* {{ font-family: "{t.family}"; }}
QWidget {{ font-size: {t.BODY}px; color: {c.TEXT}; }}
QMainWindow, QWidget#root {{ background: {c.BACKGROUND}; }}
QWidget#page, QWidget#backdrop {{ background: transparent; }}
QScrollArea, QScrollArea > QWidget > QWidget#scrollContent {{ background: transparent; border: none; }}
QLabel {{ background: transparent; }}

/* sidebar ------------------------------------------------------------------------- */
QFrame#sidebar {{ background: {c.SURFACE}; border: none; border-right: 1px solid {c.BORDER_SOFT}; }}
QLabel#appTitle {{ font-size: {t.APP_TITLE}px; font-weight: 700; }}
QLabel#appTagline {{ font-size: {t.SMALL}px; color: {c.TEXT_MUTED}; }}
QLabel#sidebarFooter {{ font-size: {t.SMALL}px; color: {c.TEXT_MUTED}; }}
QPushButton#navButton {{
    text-align: left; padding: 0 {Spacing.MD + 2}px; border: 1px solid transparent; border-radius: {r.NAV}px;
    background: transparent; color: {c.TEXT_2}; font-size: 15px; font-weight: 500;
}}
QPushButton#navButton[compact="true"] {{ padding: 0; text-align: center; }}
QPushButton#navButton:hover {{ background: {c.SURFACE_3}; color: {c.TEXT}; }}
QPushButton#navButton:checked {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {rgba(c.PRIMARY, 0.30)}, stop:1 {rgba(c.PRIMARY, 0.06)});
    border-color: {rgba(c.PRIMARY, 0.32)}; color: {c.TEXT}; font-weight: 600;
}}

/* text ---------------------------------------------------------------------------- */
QLabel#pageTitle {{ font-size: {t.PAGE_TITLE}px; font-weight: 700; }}
QLabel#pageSubtitle {{ font-size: 15px; color: {c.TEXT_2}; }}
QLabel#sectionTitle {{ font-size: {t.SECTION}px; font-weight: 600; }}
QLabel#sectionCount {{ font-size: {t.SECTION}px; font-weight: 500; color: {c.TEXT_MUTED}; }}
QLabel#fieldLabel {{ font-size: {t.SECONDARY}px; font-weight: 600; color: {c.TEXT_2}; }}
QLabel#hint, QLabel#muted {{ font-size: {t.SECONDARY}px; color: {c.TEXT_MUTED}; }}
QLabel#secondary {{ font-size: {t.SECONDARY}px; color: {c.TEXT_2}; }}
QLabel#body {{ font-size: {t.BODY}px; color: {c.TEXT_2}; }}
QLabel#dropTitle {{ font-size: 20px; font-weight: 600; }}
QLabel#dropHint {{ font-size: 15px; color: {c.TEXT_2}; }}
QLabel#formatChip {{
    font-size: {t.SMALL}px; color: {c.TEXT_MUTED}; background: {rgba("#FFFFFF", 0.04)};
    border: 1px solid {c.BORDER_SOFT}; border-radius: 12px; padding: 4px {Spacing.MD}px;
}}
QLabel#emptyTitle {{ font-size: 15px; font-weight: 600; color: {c.TEXT_2}; }}
QLabel#fileName {{ font-size: {t.BODY}px; font-weight: 600; }}
QLabel#stateText {{ font-size: {t.SECONDARY}px; font-weight: 600; color: {c.TEXT_2}; }}
QLabel#stateText[state="running"] {{ color: {c.PRIMARY_LIGHT}; }}
QLabel#stateText[state="done"] {{ color: {c.SUCCESS}; }}
QLabel#stateText[state="failed"] {{ color: {c.DANGER}; }}
QLabel#panelTitle {{ font-size: 16px; font-weight: 600; }}
QLabel#panelPercent {{ font-size: 22px; font-weight: 700; color: {c.TEXT}; }}
QLabel#statusText {{ font-size: {t.SMALL}px; color: {c.TEXT_MUTED}; }}
QLabel#statusStrong {{ font-size: {t.SMALL}px; color: {c.TEXT_2}; }}
QLabel#timeText {{ font-size: {t.SECONDARY}px; color: {c.TEXT_2}; }}
QLabel#stemName {{ font-size: {t.BODY}px; font-weight: 600; }}

/* cards --------------------------------------------------------------------------- */
QFrame#card {{ background: {c.SURFACE_2}; border: 1px solid {c.BORDER_SOFT}; border-radius: {r.CARD}px; }}
QFrame#iconTile {{ background: {rgba(c.PRIMARY, 0.10)}; border: 1px solid {rgba(c.PRIMARY, 0.20)}; border-radius: 12px; }}
QFrame#vDivider {{ background: {c.BORDER_SOFT}; border: none; }}
QFrame#statusBar {{ background: {c.SURFACE}; border: none; border-top: 1px solid {c.BORDER_SOFT}; }}

/* file rows ----------------------------------------------------------------------- */
QListWidget#fileList {{ background: transparent; border: none; outline: 0; }}
QListWidget#fileList::item, QListWidget#fileList::item:selected, QListWidget#fileList::item:hover {{
    background: transparent; border: none;
}}
QFrame#fileRow {{ background: transparent; border: 1px solid transparent; border-radius: 12px; }}
QFrame#fileRow:hover {{ background: {c.SURFACE_3}; }}
QFrame#fileRow[selected="true"] {{ background: {c.PRIMARY_TINT}; border-color: {rgba(c.PRIMARY, 0.35)}; }}
QFrame#thumb {{ background: {c.SURFACE_3}; border: 1px solid {c.BORDER_SOFT}; border-radius: 12px; }}

/* player and stem channels ---------------------------------------------------------- */
QFrame#playerRow, QFrame#stemRow {{ background: {c.SURFACE_3}; border: 1px solid {c.BORDER_SOFT}; border-radius: 14px; }}
QFrame#stemRow:hover {{ border-color: {rgba(c.PRIMARY, 0.30)}; }}
QFrame#stemRow[muted="true"] {{ background: {c.SURFACE_2}; }}
QFrame#stemTile {{ background: {rgba(c.PRIMARY, 0.12)}; border: 1px solid {rgba(c.PRIMARY, 0.22)}; border-radius: 12px; }}
QFrame#stemTile[muted="true"] {{ background: {rgba("#FFFFFF", 0.04)}; border-color: {c.BORDER_SOFT}; }}
QPushButton#playButton {{ background: {c.GRADIENT}; border: none; border-radius: 16px; padding: 0; }}
QPushButton#playButton:hover {{ background: {c.GRADIENT_HOVER}; }}
QPushButton#playButton:disabled {{ background: {c.PRIMARY_DARK}; }}
QPushButton#channelButton {{
    background: {c.ELEVATED}; border: 1px solid {c.BORDER}; border-radius: 8px; padding: 0;
    font-size: {t.SMALL}px; font-weight: 700; color: {c.TEXT_2};
}}
QPushButton#channelButton:hover {{ border-color: {c.BORDER_HOVER}; color: {c.TEXT}; }}
QPushButton#channelButton:checked {{ background: {c.PRIMARY_DARK}; border-color: {c.PRIMARY}; color: #FFFFFF; }}
QPushButton#channelButton[solo="true"]:checked {{ background: {c.PRIMARY}; border-color: {c.PRIMARY_LIGHT}; }}
QPushButton#iconButton {{ background: transparent; border: none; border-radius: 8px; padding: 0; }}
QPushButton#iconButton:hover {{ background: {c.ELEVATED}; }}
QSlider#volume::groove:horizontal {{ height: 4px; background: {c.TRACK}; border-radius: 2px; }}
QSlider#volume::sub-page:horizontal {{ background: {c.PRIMARY}; border-radius: 2px; }}
QSlider#volume::handle:horizontal {{
    background: #FFFFFF; width: 12px; height: 12px; margin: -4px 0; border-radius: 6px;
}}
QSlider#volume::handle:horizontal:hover {{ background: {c.PRIMARY_LIGHT}; }}
QSlider#volume::sub-page:horizontal:disabled {{ background: {c.WAVE_DIM}; }}

/* buttons ------------------------------------------------------------------------- */
QPushButton {{
    background: {c.SURFACE_3}; color: {c.TEXT}; border: 1px solid {c.BORDER}; border-radius: {r.BUTTON}px;
    padding: 0 {Spacing.LG + 2}px; font-size: {t.BODY}px; font-weight: 500;
}}
QPushButton:hover {{ background: {c.ELEVATED}; border-color: {c.BORDER_HOVER}; }}
QPushButton:pressed {{ background: {c.SURFACE_2}; }}
QPushButton:disabled {{ color: {c.TEXT_DISABLED}; border-color: {c.BORDER_SOFT}; background: transparent; }}
QPushButton:checked {{ background: {c.ELEVATED}; border-color: {c.BORDER_HOVER}; }}
QPushButton[variant="primary"] {{
    background: {c.GRADIENT}; color: #FFFFFF; border: none; font-weight: 600; padding: 0 {Spacing.XL}px;
}}
QPushButton[variant="primary"]:hover {{ background: {c.GRADIENT_HOVER}; }}
QPushButton[variant="primary"]:pressed {{ background: {c.PRIMARY_PRESSED}; }}
QPushButton[variant="primary"]:disabled {{ background: {rgba(c.PRIMARY, 0.22)}; color: {rgba("#FFFFFF", 0.45)}; }}
QPushButton[variant="ghost"] {{ border: none; background: transparent; color: {c.TEXT_2}; padding: 0 {Spacing.MD}px; }}
QPushButton[variant="ghost"]:hover {{ background: {c.ELEVATED}; color: {c.TEXT}; }}
QPushButton[variant="ghost"]:disabled {{ color: {c.TEXT_DISABLED}; background: transparent; }}
QPushButton[variant="danger"] {{ color: {c.DANGER}; border-color: {rgba(c.PRIMARY, 0.35)}; background: transparent; }}
QPushButton[variant="danger"]:hover {{ background: {rgba(c.PRIMARY, 0.10)}; border-color: {rgba(c.PRIMARY, 0.55)}; }}
QPushButton[variant="danger"]:disabled {{ color: {c.TEXT_DISABLED}; border-color: {c.BORDER_SOFT}; background: transparent; }}
QPushButton#rowRemove {{ border: none; background: transparent; border-radius: 8px; padding: 0; }}
QPushButton#rowRemove:hover {{ background: {rgba(c.PRIMARY, 0.14)}; }}

/* fields -------------------------------------------------------------------------- */
QLineEdit, QComboBox {{
    background: {c.SURFACE_3}; color: {c.TEXT}; border: 1px solid {c.BORDER}; border-radius: {r.INPUT}px;
    padding: 0 {Spacing.MD + 2}px; selection-background-color: {c.PRIMARY}; font-size: {t.BODY}px;
}}
QLineEdit:hover, QComboBox:hover {{ border-color: {c.BORDER_HOVER}; }}
QLineEdit:focus, QComboBox:focus, QComboBox:on {{ border-color: {c.BORDER_ACCENT}; }}
QLineEdit:disabled, QComboBox:disabled {{ color: {c.TEXT_DISABLED}; border-color: {c.BORDER_SOFT}; background: {c.SURFACE}; }}
QComboBox {{ padding-right: 40px; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 40px; border: none; }}
QComboBox::down-arrow {{ image: url("{img}/chevron.png"); width: 16px; height: 16px; }}
QComboBox::down-arrow:disabled {{ image: url("{img}/chevron-disabled.png"); }}
QComboBox QAbstractItemView {{
    background: {c.ELEVATED}; color: {c.TEXT}; border: 1px solid {c.BORDER}; border-radius: {r.INPUT}px;
    padding: 4px; outline: 0; selection-background-color: {rgba(c.PRIMARY, 0.24)};
}}
QComboBox QAbstractItemView::item {{ min-height: 36px; padding: 0 10px; border-radius: 6px; }}
QComboBox QAbstractItemView::item:selected {{ background: {rgba(c.PRIMARY, 0.24)}; color: {c.TEXT}; }}

QCheckBox {{ spacing: 12px; color: {c.TEXT}; font-size: {t.BODY}px; background: transparent; }}
QCheckBox:disabled {{ color: {c.TEXT_DISABLED}; }}
QCheckBox::indicator {{
    width: 18px; height: 18px; border: 1px solid #3A3A3A; border-radius: 5px; background: {c.SURFACE_3};
}}
QCheckBox::indicator:hover {{ border-color: {c.PRIMARY_LIGHT}; }}
QCheckBox::indicator:checked {{
    background: {c.PRIMARY}; border-color: {c.PRIMARY}; image: url("{img}/check.png");
}}
QCheckBox::indicator:disabled {{ border-color: #2A2A2A; background: {c.SURFACE}; }}
QCheckBox::indicator:checked:disabled {{ background: {c.PRIMARY_DARK}; border-color: {c.PRIMARY_DARK}; }}

QProgressBar {{ background: {c.TRACK}; border: none; border-radius: 4px; }}
QProgressBar::chunk {{
    border-radius: 4px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {c.PRIMARY_DARK}, stop:0.6 {c.PRIMARY}, stop:1 {c.PRIMARY_LIGHT});
}}

QPlainTextEdit#log {{
    background: {c.SURFACE}; color: {c.TEXT_2}; border: 1px solid {c.BORDER_SOFT}; border-radius: 12px;
    padding: 8px; selection-background-color: {c.PRIMARY};
}}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #2A2A2A; border-radius: 3px; min-height: 40px; }}
QScrollBar::handle:vertical:hover {{ background: #3A3A3A; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #2A2A2A; border-radius: 3px; min-width: 40px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QToolTip {{ background: {c.ELEVATED}; color: {c.TEXT}; border: 1px solid {c.BORDER}; padding: 6px 8px; }}
QMenu {{ background: {c.ELEVATED}; border: 1px solid {c.BORDER}; border-radius: 10px; padding: 4px; }}
QMenu::item {{ padding: 8px 24px 8px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: {rgba(c.PRIMARY, 0.24)}; }}
QMessageBox, QDialog {{ background: {c.SURFACE_2}; }}
QTextBrowser#releaseNotes {{
    background: {c.SURFACE_3}; color: {c.TEXT_2}; border: 1px solid {c.BORDER_SOFT}; border-radius: 12px; padding: 8px;
}}
QMessageBox QPushButton {{ min-width: 88px; min-height: 36px; }}
"""
