"""Self-update tests (stemsplitter/updater.py): standard library only, no network, no packaged app.

The "release" is built here from small file trees: a zip split in parts (like the Windows release), its file
index, and for the download tests a local web server that answers range requests behind a redirect (like
GitHub's release downloads). Installs run on temporary folders, with a fake self-test.

    python -m unittest discover -s tests -p test_updater.py -v
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stemsplitter import updater as U  # noqa: E402

V1 = {
    "StemSplitter.exe": b"exe v1" * 1000,
    "_internal/base_library.zip": os.urandom(50_000),
    "_internal/zeros.bin": bytes(3_000_000),  # inflates ~1000x: checks the bounded decompression
    "_internal/old_only.dll": b"removed in v2",
    "_internal/pkg/data.txt": b"same in both",
    "_internal/pkg/empty.txt": b"",
    "_internal/olddir/inner.txt": b"a folder that becomes a file in v2",
    "user-notes.txt": b"not part of the build: never touched",
}
V2 = {
    "StemSplitter.exe": b"exe v2" * 1200,
    "_internal/base_library.zip": V1["_internal/base_library.zip"],
    "_internal/zeros.bin": bytes(3_000_001),
    "_internal/pkg/data.txt": b"same in both",
    "_internal/pkg/empty.txt": b"",
    "_internal/pkg/new_module.pyd": os.urandom(20_000),
    "_internal/olddir": b"now a file",
}
BUILD_V2 = {k: v for k, v in V2.items()}  # what CI packages (user-notes.txt is not in a build)


def write_tree(root: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        p = U._ext(root / rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def snapshot(root: Path) -> dict[str, str]:
    out = {}
    for rel, p, link in U._walk(U._ext(root)):
        out[rel] = "link:" + os.readlink(p) if link else hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def make_release(folder: Path, files: dict[str, bytes], part_size: int | None = 7_000,
                 version: str = "9.0.0") -> dict:
    """dist/StemSplitter -> the zip (split in parts) and its index, like CI."""
    build = folder / "dist" / "StemSplitter"
    write_tree(build, files)
    index = U.make_index(build, "Test", version, U.archive_name("Test"))
    out = folder / "release"
    out.mkdir(parents=True, exist_ok=True)
    data_path = folder / "whole.zip"
    with zipfile.ZipFile(data_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, p, _link in sorted(U._walk(U._ext(build))):
            zf.write(p, f"StemSplitter/{rel}")
    data = data_path.read_bytes()
    name = U.archive_name("Test")
    if part_size is None:
        (out / name).write_bytes(data)
    else:
        for i in range(0, len(data), part_size):
            (out / f"{name}.{i // part_size + 1:03d}").write_bytes(data[i:i + part_size])
    (out / U.index_name("Test")).write_text(json.dumps(index), encoding="utf-8")
    return index


def as_plan_dict(plan: U.Plan) -> dict:
    return {"add": plan.add, "replace": plan.replace, "remove": plan.remove}


class VersionTests(unittest.TestCase):
    def test_parse_and_compare(self):
        self.assertEqual(U.parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(U.parse_version("1.10.0-beta"), (1, 10, 0))
        self.assertTrue(U.is_newer("v1.10.0", "1.9.9"))
        self.assertTrue(U.is_newer("1.0.1", "1.0"))
        self.assertFalse(U.is_newer("1.0.0", "1.0"))
        self.assertFalse(U.is_newer("v1.0.0", "1.0.0"))
        self.assertFalse(U.is_newer("garbage", "1.0.0"))

    def test_archive_parts_order(self):
        names = ["A.zip.010", "A.zip.002", "A.zip.001", "A.zip.txt", "B.zip"]
        self.assertEqual(U.archive_parts(names, "A.zip"), ["A.zip.001", "A.zip.002", "A.zip.010"])
        self.assertEqual(U.archive_parts(names + ["A.zip"], "A.zip"), ["A.zip"])


class IndexTests(unittest.TestCase):
    def test_bad_paths_are_rejected(self):
        base = {"format": U.INDEX_FORMAT, "root": "StemSplitter", "version": "1", "platform": "Test"}
        for rel in ["../evil.dll", "/abs", "a/../../b", "C:/x", "a\\b", "a//b"]:
            with self.subTest(rel=rel), self.assertRaises(U.UpdateError):
                U.check_index({**base, "files": {rel: {"size": 1, "sha256": "x"}}})
        for target in ["/etc/passwd", "../../outside", "../.."]:
            with self.subTest(target=target), self.assertRaises(U.UpdateError):
                U.check_index({**base, "files": {"Contents/link": {"link": target}}})
        U.check_index({**base, "files": {"Contents/Resources/lib": {"link": "../Frameworks/lib"}}})
        with self.assertRaises(U.UpdateError):
            U.check_index({**base, "format": 99, "files": {"a": {"size": 1, "sha256": "x"}}})


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="stemsplitter-updater-"))
        self.addCleanup(shutil.rmtree, U._ext(self.tmp), ignore_errors=True)
        self.index = make_release(self.tmp, BUILD_V2)
        self.parts = U.local_parts(self.tmp / "release", self.index["archive"])
        self.app = self.tmp / "apps" / "StemSplitter"
        write_tree(self.app, V1)
        self.work = U.work_dir(self.app)
        self.before = snapshot(self.app)

    def stage(self) -> U.Plan:
        plan = U.make_plan(self.app, self.index, self.work / U.NEW)
        U.download(self.parts, self.index, plan, self.work / U.NEW)
        return plan

    def expected_v2(self) -> dict[str, str]:
        files = {**V2, "user-notes.txt": V1["user-notes.txt"]}
        return {rel: hashlib.sha256(data).hexdigest() for rel, data in files.items()}

    def test_plan(self):
        plan = U.make_plan(self.app, self.index, self.work / U.NEW)
        self.assertEqual(sorted(plan.add), ["_internal/olddir", "_internal/pkg/new_module.pyd"])
        self.assertEqual(sorted(plan.replace), ["StemSplitter.exe", "_internal/zeros.bin"])
        # user-notes.txt is outside the build's own folder (_internal), so it is never removed
        self.assertEqual(plan.remove, ["_internal/old_only.dll", "_internal/olddir/inner.txt"])
        self.assertEqual(sorted(plan.fetch), sorted(plan.add + plan.replace))

    def test_full_install_into_an_empty_folder(self):
        root = self.tmp / "fresh" / "StemSplitter"
        work = U.work_dir(root)
        plan = U.make_plan(root, self.index, work / U.NEW)
        self.assertEqual(len(plan.add), len(BUILD_V2))
        U.download(self.parts, self.index, plan, work / U.NEW)
        U.apply(root, work / U.NEW, work / U.OLD, as_plan_dict(plan))
        self.assertEqual(snapshot(root), {r: hashlib.sha256(d).hexdigest() for r, d in BUILD_V2.items()})
        self.assertTrue(U.make_plan(root, self.index, work / U.NEW).empty)

    def test_single_part_archive(self):
        other = self.tmp / "single"
        index = make_release(other, BUILD_V2, part_size=None)
        parts = U.local_parts(other / "release", index["archive"])
        self.assertEqual(len(parts.parts), 1)
        plan = U.make_plan(self.app, index, self.work / U.NEW)
        U.download(parts, index, plan, self.work / U.NEW)
        U.apply(self.app, self.work / U.NEW, self.work / U.OLD, as_plan_dict(plan))
        self.assertEqual(snapshot(self.app), self.expected_v2())

    def test_update_apply_and_rollback(self):
        plan = self.stage()
        U.apply(self.app, self.work / U.NEW, self.work / U.OLD, as_plan_dict(plan))
        self.assertEqual(snapshot(self.app), self.expected_v2())
        self.assertTrue(U.make_plan(self.app, self.index, self.work / U.NEW).empty)
        U.rollback(self.app, self.work / U.NEW, self.work / U.OLD, as_plan_dict(plan))
        self.assertEqual(snapshot(self.app), self.before)

    def test_staged_files_are_reused(self):
        self.stage()
        again = U.make_plan(self.app, self.index, self.work / U.NEW)
        self.assertEqual(again.fetch, [])
        self.assertEqual(again.fetch_bytes, 0)

    def test_damaged_download_is_refused(self):
        name = "StemSplitter/_internal/pkg/new_module.pyd"  # random bytes: deflate stores them as they are
        whole = bytearray((self.tmp / "whole.zip").read_bytes())
        with zipfile.ZipFile(self.tmp / "whole.zip") as zf:
            info = zf.getinfo(name)
        whole[info.header_offset + 30 + len(name) + len(info.extra) + 5000] ^= 0xFF
        part_paths = [Path(loc) for loc, _ in self.parts.parts]
        off = 0
        for p in part_paths:
            size = p.stat().st_size
            p.write_bytes(bytes(whole[off:off + size]))
            off += size
        plan = U.make_plan(self.app, self.index, self.work / U.NEW)
        with self.assertRaisesRegex(U.UpdateError, "new_module.pyd doesn't match"):
            U.download(self.parts, self.index, plan, self.work / U.NEW)
        self.assertFalse((self.work / U.NEW / "_internal/pkg/new_module.pyd").exists())

    def _write_plan(self, plan: U.Plan) -> None:
        U._write_json(self.work / U.PLAN, {"root": str(self.app), "exe": "StemSplitter.exe", "version": "9.0.0",
                                           "from": "1.0.0", "pid": 0, **as_plan_dict(plan)})

    def test_install_success(self):
        plan = self.stage()
        self._write_plan(plan)
        seen = []
        result = U.install(self.work, seen.append, selftest=lambda exe: (exe.read_bytes() == V2["StemSplitter.exe"],
                                                                         "OK"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(snapshot(self.app), self.expected_v2())
        self.assertEqual(U._read_json(self.work / U.STATE)["state"], "done")
        self.assertIn("Checking the new version…", seen)
        self.assertTrue(U.take_result(self.app)["ok"])

    def test_install_rolls_back_when_the_selftest_fails(self):
        plan = self.stage()
        self._write_plan(plan)
        result = U.install(self.work, selftest=lambda exe: (False, "FAIL\nImportError: torch"))
        self.assertFalse(result["ok"])
        self.assertIn("ImportError: torch", result["message"])
        self.assertEqual(snapshot(self.app), self.before)

    def test_recover_after_an_interrupted_install(self):
        plan = self.stage()
        self._write_plan(plan)
        moves, real_move = [], U._move

        def crash_after_three(src, dst):
            if len(moves) == 3:
                raise KeyboardInterrupt("power cut")
            moves.append(src)
            real_move(src, dst)

        U._write_json(self.work / U.STATE, {"state": "applying", "helper": 0})
        U._move = crash_after_three
        try:
            with self.assertRaises(KeyboardInterrupt):
                U.apply(self.app, self.work / U.NEW, self.work / U.OLD, as_plan_dict(plan))
        finally:
            U._move = real_move
        self.assertNotEqual(snapshot(self.app), self.before)
        result = U.recover(self.app)
        self.assertFalse(result["ok"])
        self.assertEqual(snapshot(self.app), self.before)
        self.assertIsNone(U.recover(self.app))  # done once

    @unittest.skipUnless(os.name == "nt", "the 260-character path limit is a Windows one")
    def test_paths_longer_than_260_characters(self):
        deep = "_internal/" + "/".join(["a_long_package_folder_name"] * 10) + "/module.pyd"
        other = self.tmp / "long"
        index = make_release(other, {**BUILD_V2, deep: b"deep"}, part_size=None)
        parts = U.local_parts(other / "release", index["archive"])
        self.assertGreater(len(str(self.app / deep)), 260)
        plan = U.make_plan(self.app, index, self.work / U.NEW)
        self.assertIn(deep, plan.add)
        U.download(parts, index, plan, self.work / U.NEW)
        U.apply(self.app, self.work / U.NEW, self.work / U.OLD, as_plan_dict(plan))
        self.assertTrue(U.make_plan(self.app, index, self.work / U.NEW).empty)
        U.clone_tree(self.app, self.tmp / "clone")
        self.assertIn(deep, snapshot(self.tmp / "clone"))
        U.rollback(self.app, self.work / U.NEW, self.work / U.OLD, as_plan_dict(plan))
        self.assertEqual(snapshot(self.app), self.before)

    def test_clone_tree(self):
        clone = self.tmp / "clone" / "StemSplitter"
        U.clone_tree(self.app, clone)
        self.assertEqual(snapshot(clone), self.before)

    @unittest.skipUnless(hasattr(os, "symlink"), "no symlinks")
    def test_symlinks(self):
        try:
            os.symlink("x", self.tmp / "probe-link")
        except OSError:
            self.skipTest("creating symlinks needs a privilege here")
        files = {"Contents/MacOS/StemSplitter": b"bin", "Contents/Frameworks/lib.dylib": b"lib"}
        build = self.tmp / "mac" / "dist" / "StemSplitter.app"
        write_tree(build, files)
        os.symlink("../Frameworks/lib.dylib", build / "Contents" / "MacOS" / "lib.dylib")
        index = U.make_index(build, "Mac", "9.0.0", "x.zip")
        self.assertEqual(index["files"]["Contents/MacOS/lib.dylib"], {"link": "../Frameworks/lib.dylib"})
        root = self.tmp / "mac" / "Applications" / "StemSplitter.app"
        work = U.work_dir(root)
        plan = U.make_plan(root, index, work / U.NEW)
        U.download(None, {**index, "files": index["files"]},  # type: ignore[arg-type]
                   U.Plan(plan.version, ["Contents/MacOS/lib.dylib"], [], [], [], 0), work / U.NEW)
        self.assertEqual(os.readlink(work / U.NEW / "Contents/MacOS/lib.dylib"), "../Frameworks/lib.dylib")


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    """Serves a folder; /download/<name> redirects to /files/<name>, which answers range requests."""

    folder: Path
    api: dict
    seen: list

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.seen.append((self.path, self.headers.get("Range")))
        if self.path == "/api/latest":
            body = json.dumps(self.api).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/download/"):
            self.send_response(302)
            self.send_header("Location", "/files/" + self.path.split("/", 2)[2])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        p = self.folder / self.path.split("/", 2)[2]
        data = p.read_bytes()
        rng = self.headers.get("Range")
        if rng:
            a, b = rng.split("=")[1].split("-")
            a, b = int(a), int(b)
            body = data[a:b + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {a}-{b}/{len(data)}")
        else:
            body = data
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="stemsplitter-updater-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # a large unchanged file, so "only what changed is downloaded" is measurable against the tail read
        self.big = {"_internal/torch_cuda.dll": os.urandom(4_000_000)}
        make_release(self.tmp, {**BUILD_V2, **self.big}, part_size=1_500_000)
        release_dir = self.tmp / "release"
        handler = type("H", (_RangeHandler,), {"folder": release_dir, "seen": [], "api": {}})
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        handler.api = {"tag_name": "v9.0.0", "body": "* faster", "html_url": base + "/page",
                       "assets": [{"name": p.name, "size": p.stat().st_size,
                                   "browser_download_url": f"{base}/download/{p.name}"}
                                  for p in sorted(release_dir.iterdir())]}
        self.handler = handler
        self.old_api, U.API_LATEST = U.API_LATEST, base + "/api/latest"
        self.addCleanup(setattr, U, "API_LATEST", self.old_api)

    def test_check_and_prepare_over_http(self):
        release = U.fetch_latest()
        self.assertEqual((release.version, release.notes), ("9.0.0", "* faster"))
        self.assertTrue(U.is_newer(release.version, "1.0.0"))
        app = self.tmp / "apps" / "StemSplitter"
        write_tree(app, {**V1, **self.big})
        inst = U.Installation(root=app, exe="StemSplitter.exe", platform="Test")
        self.assertIsNone(U.self_update_problem(inst, release))
        self.assertIn("no update files", U.self_update_problem(U.Installation(app, "x", "Other"), release))
        progress = []
        plan = U.prepare(inst, release, lambda f, t: progress.append(t))
        self.assertEqual(sorted(plan.fetch), sorted(plan.add + plan.replace))
        U.apply(app, inst.work / U.NEW, inst.work / U.OLD, as_plan_dict(plan))
        files = {**V2, **self.big, "user-notes.txt": V1["user-notes.txt"]}
        self.assertEqual(snapshot(app), {r: hashlib.sha256(d).hexdigest() for r, d in files.items()})
        ranges = [r for path, r in self.handler.seen if path.startswith("/files/") and ".zip." in path]
        self.assertTrue(ranges and all(r and r.startswith("bytes=") for r in ranges))

    def test_only_changed_files_are_downloaded(self):
        release = U.fetch_latest()
        app = self.tmp / "apps" / "StemSplitter"
        write_tree(app, {**BUILD_V2, **self.big, "StemSplitter.exe": b"older exe"})
        inst = U.Installation(root=app, exe="StemSplitter.exe", platform="Test")
        self.handler.seen.clear()
        plan = U.prepare(inst, release)
        self.assertEqual((plan.add, plan.replace, plan.remove), ([], ["StemSplitter.exe"], []))
        fetched = 0
        for path, rng in self.handler.seen:
            if path.startswith("/files/") and ".zip." in path:
                a, b = rng.split("=")[1].split("-")
                fetched += int(b) - int(a) + 1
        total = sum(p.stat().st_size for p in (self.tmp / "release").glob("*.zip.*"))
        self.assertLess(fetched, 1.2e6)  # the last MB (the directory) and the small file, not the 4 MB archive
        self.assertGreater(total, 4e6)

    def test_insecure_urls_are_refused(self):
        with self.assertRaises(U.UpdateError):
            U._check_url("http://example.com/file.zip")
        U._check_url("https://github.com/x")


if __name__ == "__main__":
    unittest.main()
