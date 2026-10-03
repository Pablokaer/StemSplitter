"""Interface languages.

Every user-visible text is written in English in the code and passed through `tr()`, which looks it up in
the catalog of the current language (`stemsplitter/locales/<code>.py`: English text -> translation) and fills
its `{placeholders}`. A text missing from a catalog shows in English. The language is set once at start-up
(`set_language`), before any widget is built; changing it in Settings takes effect after a restart.

The engine and the worker keep reporting in English (the log stays in English too): `status_text()` and
`device_text()` translate their progress and device texts when the window shows them.

Standard library only: the updater (and through it the CI release tools) uses `tr()` too.
"""

from __future__ import annotations

import re

from .locales import es, hi, pt, zh

# code -> name of the language in that language (what the Settings menu shows)
LANGUAGES = {
    "en": "English",
    "pt": "Português (Brasil)",
    "es": "Español",
    "zh": "中文（简体）",
    "hi": "हिन्दी",
}
_CATALOGS = {"pt": pt.MESSAGES, "es": es.MESSAGES, "zh": zh.MESSAGES, "hi": hi.MESSAGES}

_language = "en"
_catalog: dict[str, str] = {}


def set_language(code: str) -> str:
    """Use `code` (falls back to English); returns the code in use."""
    global _language, _catalog
    _language = code if code in LANGUAGES else "en"
    _catalog = _CATALOGS.get(_language, {})
    return _language


def language() -> str:
    return _language


def system_language() -> str:
    """The supported language closest to the system's, or English."""
    names = []
    try:
        from PySide6.QtCore import QLocale

        names = [n.replace("-", "_") for n in QLocale.system().uiLanguages()]
    except Exception:  # no Qt (CLI, CI): the C library's locale
        import locale

        names = [locale.getlocale()[0] or ""]
    for name in names:
        code = name.split("_")[0].lower()
        if code in LANGUAGES:
            return code
    return "en"


def N_(text: str) -> str:
    """Marks a text for translation without translating it yet (a constant, translated where it is shown)."""
    return text


def tr(text: str, **values) -> str:
    """`text` in the current language, with `{name}` placeholders filled from `values`."""
    out = _catalog.get(text, text)
    return out.format(**values) if values else out


def tr_n(one: str, other: str, n: int, **values) -> str:
    """The singular or plural form for the count `n` (available as `{n}`)."""
    singular = n == 1 or (_language == "hi" and n == 0)  # Hindi treats 0 like 1
    return tr(one if singular else other, n=n, **values)


# -- texts that come from the worker process (always English there) --------------------
_MODELS = {"stem model": N_("stem model"), "vocal model": N_("vocal model"), "model": N_("model")}
_STAGES = {"Separating stems": N_("Separating stems"), "Refining vocals": N_("Refining vocals")}
_STATUS = [
    (re.compile(r"Starting engine \(loading PyTorch\)\.\.\.$"), lambda m: tr("Starting engine (loading PyTorch)...")),
    (re.compile(r"Decoding audio\.\.\.$"), lambda m: tr("Decoding audio...")),
    (re.compile(r"Downloading (stem model|vocal model|model) \(first run only\): (\d+) MB(?: \((\d+)%\))?$"),
     lambda m: tr("Downloading the {model} (first run only): {mb} MB", model=tr(_MODELS[m[1]]), mb=m[2])
     + (f" ({m[3]}%)" if m[3] else "")),
    (re.compile(r"Loading (stem model|vocal model|model)\.\.\.$"),
     lambda m: tr("Loading the {model}...", model=tr(_MODELS[m[1]]))),
    (re.compile(r"Models ready$"), lambda m: tr("Models ready")),
    (re.compile(r"(Separating stems|Refining vocals)\.\.\.(?: (\d+)%)?$"),
     lambda m: tr(_STAGES[m[1]]) + "..." + (f" {m[2]}%" if m[2] else "")),
    (re.compile(r"Assembling stems\.\.\.$"), lambda m: tr("Assembling stems...")),
    (re.compile(r"Writing files\.\.\.(?: (\d+)/(\d+))?$"),
     lambda m: tr("Writing files...") + (f" {m[1]}/{m[2]}" if m[1] else "")),
    (re.compile(r"Done$"), lambda m: tr("Done")),
    (re.compile(r"Waiting for free memory \((\d+) MB more needed\)\.\.\.$"),
     lambda m: tr("Waiting for free memory ({mb} MB more needed)...", mb=m[1])),
]
_DEVICE = [
    (re.compile(r"NVIDIA GPU · (.+)$"), lambda m: tr("NVIDIA GPU · {name}", name=m[1])),
    (re.compile(r"Apple Silicon GPU \(Metal\)$"), lambda m: tr("Apple Silicon GPU (Metal)")),
    (re.compile(r"CPU · (\d+) threads \(no GPU found - this will be slow\)$"),
     lambda m: tr("CPU · {n} threads (no GPU found - this will be slow)", n=m[1])),
]


def _match(rules, text: str) -> str:
    for pattern, render in rules:
        m = pattern.match(text)
        if m:
            return render(m)
    return text


def status_text(text: str) -> str:
    """A progress text from the engine or the worker, in the current language (unknown texts as they are)."""
    return _match(_STATUS, text)


def device_text(text: str) -> str:
    return _match(_DEVICE, text)
