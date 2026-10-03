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
    BG = "#090D16"  # main background
    BG_2 = "#0D1320"  # secondary background (status bar, drop zone)
    SIDEBAR = "#0C1220"
    CARD = "#111827"
    CARD_2 = "#151D2C"
    FIELD = "#111723"
    BORDER = "#293246"  # fields and buttons
    BORDER_SOFT = "#1C2434"  # cards and dividers
    BORDER_HOVER = "#3A4560"
    BORDER_ACCENT = "#7457FF"
    PURPLE = "#7657FF"
    PURPLE_HOVER = "#8468FF"
    PURPLE_PRESSED = "#6446F0"
    PURPLE_LIGHT = "#9B7BFF"
    PURPLE_PALE = "#C5B6FF"
    PURPLE_TINT = "#1A1738"  # selected row / drag-over background
    PURPLE_TINT_BORDER = "#4B3AA8"
    TEXT = "#F5F7FB"
    TEXT_2 = "#B2BACB"
    TEXT_MUTED = "#778197"
    TEXT_DISABLED = "#4B5468"
    TRACK = "#1F2636"  # empty part of progress bars and rings
    GREEN = "#49D17D"
    RED = "#FF6464"


class Spacing:
    XS = 4
    SM = 8
    MD = 12
    LG = 16
    XL = 24
    XXL = 32


class Radii:
    INPUT = 8
    BUTTON = 10
    NAV = 10
    CARD = 14
    DROP = 16


class Type:
    """Pixel sizes; the family is picked at startup by `pick_font_family()`."""

    PAGE_TITLE = 32
    APP_TITLE = 24
    SECTION = 17
    BODY = 14
    SECONDARY = 13
    SMALL = 12
    family = "Segoe UI"


class Sizes:
    WINDOW_MIN = (1100, 700)  # also the size the window opens at
    SIDEBAR = 248
    CONTROL = 44  # line edits and combo boxes
    BUTTON = 44
    BUTTON_PRIMARY = 48
    BUTTON_SMALL = 36
    ROW = 68  # one file in the queue
    DROP_ZONE = 210
    PROGRESS = 8


PREFERRED_FONTS = ["Inter", "Inter Variable", "Segoe UI Variable Text", "Segoe UI", "SF Pro Text",
                   ".AppleSystemUIFont", "Helvetica Neue", "Arial"]


def pick_font_family() -> str:
    families = set(QFontDatabase.families())
    for name in PREFERRED_FONTS:
        if name in families:
            Type.family = name
            break
    return Type.family


def dark_palette() -> QPalette:
    """For the few things the style sheet doesn't reach (Fusion-drawn frames, some dialogs)."""
    p = QPalette()
    roles = {
        QPalette.Window: Colors.BG, QPalette.WindowText: Colors.TEXT, QPalette.Base: Colors.FIELD,
        QPalette.AlternateBase: Colors.CARD, QPalette.Text: Colors.TEXT, QPalette.Button: Colors.CARD_2,
        QPalette.ButtonText: Colors.TEXT, QPalette.Highlight: Colors.PURPLE, QPalette.HighlightedText: "#FFFFFF",
        QPalette.ToolTipBase: Colors.CARD_2, QPalette.ToolTipText: Colors.TEXT, QPalette.PlaceholderText: Colors.TEXT_MUTED,
        QPalette.Mid: Colors.BORDER, QPalette.Midlight: Colors.BORDER_SOFT, QPalette.Link: Colors.PURPLE_LIGHT,
    }
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor(Colors.TEXT_DISABLED))
    return p


def _write_style_images() -> Path:
    """The style sheet needs image files for the combo arrow and the check mark: draw them once per run."""
    from .icons import pixmap

    folder = Path(tempfile.mkdtemp(prefix="stemsplitter-ui-"))
    atexit.register(shutil.rmtree, folder, True)
    for name, icon, color, size in [("chevron", "chevron-down", Colors.TEXT_2, 16),
                                    ("chevron-disabled", "chevron-down", Colors.TEXT_DISABLED, 16),
                                    ("check", "check-bold", Colors.BG, 14)]:
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
QMainWindow, QWidget#root, QWidget#page {{ background: {c.BG}; }}
QScrollArea, QScrollArea > QWidget > QWidget#scrollContent {{ background: transparent; border: none; }}
QLabel {{ background: transparent; }}

/* sidebar ------------------------------------------------------------------------- */
QFrame#sidebar {{ background: {c.SIDEBAR}; border: none; border-right: 1px solid {c.BORDER_SOFT}; }}
QLabel#appTitle {{ font-size: {t.APP_TITLE}px; font-weight: 700; }}
QLabel#appTagline {{ font-size: {t.SECONDARY}px; color: {c.TEXT_MUTED}; }}
QLabel#sidebarFooter {{ font-size: {t.SMALL}px; color: {c.TEXT_MUTED}; }}
QPushButton#navButton {{
    text-align: left; padding: 0 14px; border: none; border-radius: {r.NAV}px;
    background: transparent; color: {c.TEXT_2}; font-size: 15px; font-weight: 500;
}}
QPushButton#navButton:hover {{ background: #141B2B; color: {c.TEXT}; }}
QPushButton#navButton:checked {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #4D7657FF, stop:1 #147657FF);
    color: {c.TEXT}; font-weight: 600;
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
QLabel#dropTitle {{ font-size: 18px; font-weight: 600; }}
QLabel#emptyTitle {{ font-size: 15px; font-weight: 600; color: {c.TEXT_2}; }}
QLabel#fileName {{ font-size: {t.BODY}px; font-weight: 600; }}
QLabel#stateText {{ font-size: {t.SECONDARY}px; font-weight: 600; color: {c.TEXT_2}; }}
QLabel#stateText[state="running"] {{ color: {c.PURPLE_PALE}; }}
QLabel#stateText[state="done"] {{ color: {c.GREEN}; }}
QLabel#stateText[state="failed"] {{ color: {c.RED}; }}
QLabel#panelTitle {{ font-size: 16px; font-weight: 600; }}
QLabel#panelPercent {{ font-size: 22px; font-weight: 700; color: {c.TEXT}; }}
QLabel#statusText {{ font-size: {t.SMALL}px; color: {c.TEXT_MUTED}; }}
QLabel#statusStrong {{ font-size: {t.SMALL}px; color: {c.TEXT_2}; }}

/* cards --------------------------------------------------------------------------- */
QFrame#card {{ background: {c.CARD}; border: 1px solid {c.BORDER_SOFT}; border-radius: {r.CARD}px; }}
QFrame#iconTile {{ background: #1C1940; border: none; border-radius: 12px; }}
QFrame#vDivider {{ background: {c.BORDER_SOFT}; border: none; }}
QFrame#statusBar {{ background: {c.BG_2}; border: none; border-top: 1px solid {c.BORDER_SOFT}; }}

/* file rows ----------------------------------------------------------------------- */
QListWidget#fileList {{ background: transparent; border: none; outline: 0; }}
QListWidget#fileList::item, QListWidget#fileList::item:selected, QListWidget#fileList::item:hover {{
    background: transparent; border: none;
}}
QFrame#fileRow {{ background: transparent; border: 1px solid transparent; border-radius: 10px; }}
QFrame#fileRow:hover {{ background: {c.CARD_2}; }}
QFrame#fileRow[selected="true"] {{ background: {c.PURPLE_TINT}; border-color: {c.PURPLE_TINT_BORDER}; }}
QFrame#thumb {{ background: {c.CARD_2}; border: 1px solid {c.BORDER_SOFT}; border-radius: 10px; }}

/* buttons ------------------------------------------------------------------------- */
QPushButton {{
    background: transparent; color: {c.TEXT}; border: 1px solid {c.BORDER}; border-radius: {r.BUTTON}px;
    padding: 0 18px; font-size: {t.BODY}px; font-weight: 500;
}}
QPushButton:hover {{ background: #182033; border-color: {c.BORDER_HOVER}; }}
QPushButton:pressed {{ background: #0F1522; }}
QPushButton:disabled {{ color: {c.TEXT_DISABLED}; border-color: #1C2333; }}
QPushButton:checked {{ background: {c.CARD_2}; border-color: {c.BORDER_HOVER}; }}
QPushButton[variant="primary"] {{
    background: {c.PURPLE}; color: #FFFFFF; border: none; font-weight: 600; padding: 0 24px;
}}
QPushButton[variant="primary"]:hover {{ background: {c.PURPLE_HOVER}; }}
QPushButton[variant="primary"]:pressed {{ background: {c.PURPLE_PRESSED}; }}
QPushButton[variant="primary"]:disabled {{ background: #2C2552; color: #8D86B0; }}
QPushButton[variant="ghost"] {{ border: none; color: {c.TEXT_2}; padding: 0 12px; }}
QPushButton[variant="ghost"]:hover {{ background: {c.CARD_2}; color: {c.TEXT}; }}
QPushButton[variant="ghost"]:disabled {{ color: {c.TEXT_DISABLED}; background: transparent; }}
QPushButton[variant="danger"] {{ color: {c.RED}; border-color: #4A2A33; }}
QPushButton[variant="danger"]:hover {{ background: #2A1720; border-color: #6B3340; }}
QPushButton[variant="danger"]:disabled {{ color: {c.TEXT_DISABLED}; border-color: #1C2333; background: transparent; }}
QPushButton#rowRemove {{ border: none; border-radius: 8px; padding: 0; }}
QPushButton#rowRemove:hover {{ background: #2A1720; }}

/* fields -------------------------------------------------------------------------- */
QLineEdit, QComboBox {{
    background: {c.FIELD}; color: {c.TEXT}; border: 1px solid {c.BORDER}; border-radius: {r.INPUT}px;
    padding: 0 14px; selection-background-color: {c.PURPLE}; font-size: {t.BODY}px;
}}
QLineEdit:hover, QComboBox:hover {{ border-color: {c.BORDER_HOVER}; }}
QLineEdit:focus, QComboBox:focus, QComboBox:on {{ border-color: {c.BORDER_ACCENT}; }}
QLineEdit:disabled, QComboBox:disabled {{ color: {c.TEXT_DISABLED}; border-color: #1C2333; background: #0E131D; }}
QComboBox {{ padding-right: 40px; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 40px; border: none; }}
QComboBox::down-arrow {{ image: url("{img}/chevron.png"); width: 16px; height: 16px; }}
QComboBox::down-arrow:disabled {{ image: url("{img}/chevron-disabled.png"); }}
QComboBox QAbstractItemView {{
    background: {c.CARD_2}; color: {c.TEXT}; border: 1px solid {c.BORDER}; border-radius: {r.INPUT}px;
    padding: 4px; outline: 0; selection-background-color: #2A2350;
}}
QComboBox QAbstractItemView::item {{ min-height: 36px; padding: 0 10px; border-radius: 6px; }}
QComboBox QAbstractItemView::item:selected {{ background: #2A2350; color: {c.TEXT}; }}

QCheckBox {{ spacing: 12px; color: {c.TEXT}; font-size: {t.BODY}px; background: transparent; }}
QCheckBox:disabled {{ color: {c.TEXT_DISABLED}; }}
QCheckBox::indicator {{
    width: 18px; height: 18px; border: 1px solid #3A4459; border-radius: 5px; background: {c.FIELD};
}}
QCheckBox::indicator:hover {{ border-color: {c.PURPLE_LIGHT}; }}
QCheckBox::indicator:checked {{
    background: {c.PURPLE_LIGHT}; border-color: {c.PURPLE_LIGHT}; image: url("{img}/check.png");
}}
QCheckBox::indicator:disabled {{ border-color: #252D3D; background: #0E131D; }}
QCheckBox::indicator:checked:disabled {{ background: #4A3F80; border-color: #4A3F80; }}

QProgressBar {{ background: {c.TRACK}; border: none; border-radius: 4px; }}
QProgressBar::chunk {{
    border-radius: 4px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {c.PURPLE}, stop:1 {c.PURPLE_PALE});
}}

QPlainTextEdit#log {{
    background: #0B101B; color: {c.TEXT_2}; border: 1px solid {c.BORDER_SOFT}; border-radius: 10px;
    padding: 8px; selection-background-color: {c.PURPLE};
}}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #263044; border-radius: 3px; min-height: 40px; }}
QScrollBar::handle:vertical:hover {{ background: #334059; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #263044; border-radius: 3px; min-width: 40px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QToolTip {{ background: {c.CARD_2}; color: {c.TEXT}; border: 1px solid {c.BORDER}; padding: 6px 8px; }}
QMessageBox, QDialog {{ background: {c.CARD}; }}
QTextBrowser#releaseNotes {{
    background: {c.FIELD}; color: {c.TEXT_2}; border: 1px solid {c.BORDER_SOFT}; border-radius: 10px; padding: 8px;
}}
QMessageBox QPushButton {{ min-width: 88px; min-height: 36px; }}
"""
