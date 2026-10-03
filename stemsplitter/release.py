"""Release tools for CI (standard library only).

    python -m stemsplitter.release check-version v1.2.0
    python -m stemsplitter.release index dist/StemSplitter --platform Windows-x64 -o release/
    python -m stemsplitter.release verify release/StemSplitter-Windows-x64.files.json --into verify/

`index` writes the build's file index next to the release archive. `verify` checks the archive with the same
code the app updates itself with: it installs the whole build from the archive into an empty folder, then
damages that copy (a file deleted, one changed, a stray file added) and updates it again; after each step
the copy must match the index exactly. CI then runs `--selftest` on the copy.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from . import __version__
from . import updater as U


def check_version(tag: str) -> int:
    if tag.lstrip("vV") != __version__:
        print(f"::error::The tag {tag} doesn't match the app version {__version__} (stemsplitter/__init__.py)")
        return 1
    print(f"tag {tag} matches the app version {__version__}")
    return 0


def write_index(root: Path, platform: str, out: Path) -> int:
    t0 = time.perf_counter()
    index = U.make_index(root, platform, __version__, U.archive_name(platform))
    out.mkdir(parents=True, exist_ok=True)
    path = out / U.index_name(platform)
    path.write_text(json.dumps(index, indent=0, sort_keys=True), encoding="utf-8")
    files = index["files"].values()
    size = sum(m.get("size", 0) for m in files)
    print(f"{path}: {len(index['files'])} entries, {size / 1e9:.2f} GB, {time.perf_counter() - t0:.0f} s")
    return 0


def _update(root: Path, index: dict, parts: U.Parts, work: Path) -> U.Plan:
    staging, backup = work / U.NEW, work / U.OLD
    plan = U.make_plan(root, index, staging)
    U.download(parts, index, plan, staging)
    U.apply(root, staging, backup, {"add": plan.add, "replace": plan.replace, "remove": plan.remove})
    shutil.rmtree(U._ext(work))
    return plan


def _expect_clean(root: Path, index: dict, work: Path, step: str) -> None:
    left = U.make_plan(root, index, work / U.NEW)
    if not left.empty:
        raise SystemExit(f"::error::after {step} the copy still differs from the index: add={left.add[:5]} "
                         f"replace={left.replace[:5]} remove={left.remove[:5]}")


def verify(index_path: Path, into: Path) -> int:
    t0 = time.perf_counter()
    index = U.check_index(json.loads(index_path.read_text(encoding="utf-8")))
    parts = U.local_parts(index_path.parent, index["archive"])
    if into.exists():
        shutil.rmtree(U._ext(into))
    root, work = into / index["root"], into / "work"
    plan = _update(root, index, parts, work)
    print(f"installed {len(plan.add)} entries from {len(parts.parts)} part(s) ({parts.size / 1e9:.2f} GB) "
          f"in {time.perf_counter() - t0:.0f} s")
    _expect_clean(root, index, work, "the full install")

    files = [rel for rel, m in index["files"].items() if "link" not in m and m["size"] > 0]
    gone, changed = files[0], files[-1]
    (root / gone).unlink()
    (root / changed).write_bytes(b"damaged")
    managed = sorted({rel.split("/", 1)[0] for rel in index["files"] if "/" in rel})[0]
    stray = root / managed / "stray-file-from-an-old-version.txt"
    stray.write_bytes(b"old")
    plan = _update(root, index, parts, work)
    expected = ([gone], [changed], [stray.relative_to(root).as_posix()])
    if (plan.add, plan.replace, plan.remove) != expected:
        print(f"::error::the update planned add={plan.add} replace={plan.replace} remove={plan.remove}, "
              f"expected {expected}")
        return 1
    _expect_clean(root, index, work, "the partial update")
    print(f"partial update OK (1 added, 1 replaced, 1 removed), {time.perf_counter() - t0:.0f} s in total")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m stemsplitter.release")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check-version", help="fail unless the tag (vX.Y.Z) matches __version__")
    p.add_argument("tag")
    p = sub.add_parser("index", help="write <out>/StemSplitter-<platform>.files.json for a build folder")
    p.add_argument("root", type=Path, help="dist/StemSplitter or dist/StemSplitter.app")
    p.add_argument("--platform", required=True)
    p.add_argument("-o", "--out", type=Path, required=True)
    p = sub.add_parser("verify", help="install the build from its archive with the updater's code and check it")
    p.add_argument("index", type=Path)
    p.add_argument("--into", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.cmd == "check-version":
        return check_version(args.tag)
    if args.cmd == "index":
        return write_index(args.root, args.platform, args.out)
    return verify(args.index, args.into)


if __name__ == "__main__":
    sys.exit(main())
