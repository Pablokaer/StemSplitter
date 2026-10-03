"""Interface language tests (stemsplitter/i18n.py and stemsplitter/locales/): standard library only.

Every text passed to tr(), tr_n() or N_() in the code must be in every catalog, no catalog may keep texts the
code no longer uses, and each translation must have the same {placeholders} as the English text. The engine's
progress texts (always English, from the worker process) must still match the patterns that translate them.

    python -m unittest discover -s tests -p test_i18n.py -v
"""

from __future__ import annotations

import ast
import string
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stemsplitter import i18n  # noqa: E402

CALLS = {"tr": 1, "N_": 1, "tr_n": 2}  # function -> how many leading arguments are texts


def source_texts() -> dict[str, str]:
    """Every literal text passed to tr / tr_n / N_ in the app's code -> the file it is in."""
    found: dict[str, str] = {}
    files = [p for p in (ROOT / "stemsplitter").rglob("*.py") if "locales" not in p.parts] + [ROOT / "main.py"]
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
            for arg in node.args[:CALLS.get(name, 0)]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.setdefault(arg.value, path.relative_to(ROOT).as_posix())
    return found


def fields(text: str) -> list[tuple[str, str]]:
    return sorted((name, spec) for _, name, spec, _ in string.Formatter().parse(text) if name is not None)


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(i18n.set_language, "en")

    def test_every_language_has_exactly_the_texts_of_the_code(self):
        texts = source_texts()
        self.assertGreater(len(texts), 150)
        for code, catalog in i18n._CATALOGS.items():
            with self.subTest(language=code):
                missing = sorted(set(texts) - set(catalog))
                self.assertEqual(missing, [], f"{code}: not translated (first in {texts.get(missing[0]) if missing else ''})")
                self.assertEqual(sorted(set(catalog) - set(texts)), [], f"{code}: texts the code no longer uses")

    def test_placeholders_match(self):
        for code, catalog in i18n._CATALOGS.items():
            for english, translated in catalog.items():
                with self.subTest(language=code, text=english):
                    self.assertEqual(fields(translated), fields(english))
                    self.assertTrue(translated.strip())

    def test_every_language_is_listed_with_its_catalog(self):
        self.assertEqual(set(i18n.LANGUAGES), {"en", *i18n._CATALOGS})
        self.assertGreaterEqual(len(i18n.LANGUAGES), 5)


class TranslateTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(i18n.set_language, "en")

    def test_tr_and_fallback(self):
        self.assertEqual(i18n.set_language("pt"), "pt")
        self.assertEqual(i18n.tr("Split Stems"), "Separar stems")
        self.assertEqual(i18n.tr("Version {version}", version="1.2.0"), "Versão 1.2.0")
        self.assertEqual(i18n.tr("A text nobody translated"), "A text nobody translated")
        self.assertEqual(i18n.set_language("xx"), "en")
        self.assertEqual(i18n.tr("Split Stems"), "Split Stems")

    def test_plurals(self):
        i18n.set_language("es")
        self.assertEqual(i18n.tr_n("{n} file", "{n} files", 1), "1 archivo")
        self.assertEqual(i18n.tr_n("{n} file", "{n} files", 3), "3 archivos")
        i18n.set_language("en")
        self.assertEqual(i18n.tr_n("{n} file", "{n} files", 0), "0 files")

    def test_engine_texts_are_translated(self):
        i18n.set_language("pt")
        cases = {
            "Starting engine (loading PyTorch)...": "Iniciando o motor (carregando o PyTorch)...",
            "Decoding audio...": "Decodificando o áudio...",
            "Downloading stem model (first run only): 350 MB": "Baixando o modelo de stems (só na primeira vez): 350 MB",
            "Downloading model (first run only): 12 MB (30%)": "Baixando o modelo (só na primeira vez): 12 MB (30%)",
            "Loading vocal model...": "Carregando o modelo de voz...",
            "Loading model...": "Carregando o modelo...",
            "Models ready": "Modelos prontos",
            "Separating stems...": "Separando os stems...",
            "Separating stems... 45%": "Separando os stems... 45%",
            "Refining vocals... 7%": "Refinando a voz... 7%",
            "Assembling stems...": "Montando os stems...",
            "Writing files...": "Gravando os arquivos...",
            "Writing files... 2/4": "Gravando os arquivos... 2/4",
            "Done": "Pronto",
            "Waiting for free memory (512 MB more needed)...": "Aguardando memória livre (faltam 512 MB)...",
            "Something new from a later engine": "Something new from a later engine",
        }
        for english, expected in cases.items():
            with self.subTest(text=english):
                self.assertEqual(i18n.status_text(english), expected)
        self.assertEqual(i18n.device_text("NVIDIA GPU · NVIDIA GeForce RTX 3050"), "GPU NVIDIA · NVIDIA GeForce RTX 3050")
        self.assertEqual(i18n.device_text("CPU · 12 threads (no GPU found - this will be slow)"),
                         "CPU · 12 threads (nenhuma GPU encontrada - vai ser lento)")

    def test_the_engine_still_writes_the_texts_the_patterns_expect(self):
        """If one of these changes in the engine or the worker, update the patterns in i18n.py too."""
        engine = (ROOT / "stemsplitter" / "engine.py").read_text(encoding="utf-8")
        worker = (ROOT / "stemsplitter" / "worker.py").read_text(encoding="utf-8")
        for snippet in ['"Decoding audio..."', '"Separating stems"', '"Refining vocals"', '"Assembling stems..."',
                        '"Writing files..."', 'f"Writing files... {finished}/{len(jobs)}"', '"Models ready"', '"Done"',
                        '"Downloading {label} (first run only): {mb:.0f} MB"', 'f"Loading {label}..."',
                        '"Downloading model (first run only): {mb:.0f} MB ({frac:.0%})"', '"Loading model..."',
                        '"stem model"', '"vocal model"', '"Waiting for free memory ({max(short, 0) / MB:.0f} MB more '
                        'needed)..."', 'f"{label}... {k / len(starts):.0%}"']:
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, engine)
        for snippet in ['"Starting engine (loading PyTorch)..."', 'f"NVIDIA GPU · {torch.cuda.get_device_name(0)}"',
                        '"Apple Silicon GPU (Metal)"',
                        'f"CPU · {torch.get_num_threads()} threads (no GPU found - this will be slow)"']:
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, worker)

    def test_system_language_is_supported(self):
        self.assertIn(i18n.system_language(), i18n.LANGUAGES)


if __name__ == "__main__":
    unittest.main()
