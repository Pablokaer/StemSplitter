"""Start a release: bump the version, commit it, tag it and push (the tag makes CI build and publish).

    python tools/release.py 1.2.0        # an explicit version
    python tools/release.py patch        # 1.1.2 -> 1.1.3   (also: minor, major)
    python tools/release.py minor --dry-run

It refuses to run unless you are on `main`, the working tree is clean, `main` matches `origin/main`
and the tag doesn't exist yet. Standard library only.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "stemsplitter" / "__init__.py"
VERSION_RE = re.compile(r'^__version__ = "(\d+)\.(\d+)\.(\d+)"$', re.M)


def git(*args: str, check: bool = True) -> str:
    done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if check and done.returncode:
        sys.exit(f"git {' '.join(args)} failed:\n{done.stderr.strip()}")
    return done.stdout.strip()


def next_version(current: tuple[int, int, int], what: str) -> str:
    major, minor, patch = current
    if what == "major":
        return f"{major + 1}.0.0"
    if what == "minor":
        return f"{major}.{minor + 1}.0"
    if what == "patch":
        return f"{major}.{minor}.{patch + 1}"
    if not re.fullmatch(r"\d+\.\d+\.\d+", what):
        sys.exit(f"'{what}' is not a version (X.Y.Z) or one of patch, minor, major")
    if tuple(map(int, what.split("."))) <= current:
        sys.exit(f"{what} is not newer than the current version {'.'.join(map(str, current))}")
    return what


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("version", help="X.Y.Z, or patch / minor / major")
    parser.add_argument("--dry-run", action="store_true", help="check everything and say what would happen")
    args = parser.parse_args()

    text = INIT.read_text(encoding="utf-8")
    match = VERSION_RE.search(text)
    if not match:
        sys.exit("could not find __version__ in stemsplitter/__init__.py")
    version = next_version(tuple(map(int, match.groups())), args.version)
    tag = f"v{version}"

    if git("branch", "--show-current") != "main":
        sys.exit("releases are made from main: check it out first")
    if git("status", "--porcelain", "--untracked-files=no"):
        sys.exit("the working tree has uncommitted changes: commit or stash them first")
    git("fetch", "origin", "main", "--tags")
    if git("rev-parse", "HEAD") != git("rev-parse", "origin/main"):
        sys.exit("main differs from origin/main: pull (or push) first")
    if git("tag", "--list", tag):
        sys.exit(f"the tag {tag} already exists")

    print(f"{match.group(0)}  ->  __version__ = \"{version}\"; tag {tag}")
    if args.dry_run:
        print("dry run: nothing changed")
        return
    INIT.write_text(VERSION_RE.sub(f'__version__ = "{version}"', text, count=1), encoding="utf-8")
    git("add", str(INIT))
    git("commit", "-m", f"release: version {version}")
    git("tag", tag)
    git("push", "origin", "main", tag)
    print(f"pushed {tag}: CI now builds it and publishes the release (edit its notes on GitHub).")


if __name__ == "__main__":
    main()
