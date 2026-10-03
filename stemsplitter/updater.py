"""Self-update from GitHub Releases (docs/DOCUMENTATION.md, section 5.7).

1. **Check:** the GitHub API names the latest published release; its tag is compared with `__version__`.
2. **Plan:** each release has a file index per build (`StemSplitter-<platform>.files.json`: path, size and
   SHA-256 of every file of the packaged app). The installed files are hashed and only the ones that differ
   are needed, so an update that only changes the app's code downloads a few MB, not the whole 2+ GB build.
3. **Download:** the release archive is a zip (split in parts on Windows, where a release file can't exceed
   2 GB). Its central directory is read with HTTP range requests and only the needed members are downloaded,
   unpacked and checked against the index, into a staging folder next to the app (`.ss-update/n`).
4. **Install:** the running app can't replace its own files: its Python code is read from the executable
   while it runs, so a replaced executable breaks the next import. It hard-links a copy of itself into the work
   folder, starts that copy as a helper (`--apply-update`) and quits. The helper moves the old files aside and
   the new ones in, runs the new version's `--selftest`, puts the old files back if anything fails, and starts
   the app again. The app shows the result once and removes the work folder.

Only the standard library is imported at module level: CI runs the release tools (`stemsplitter.release`)
without the app's dependencies.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import os
import posixpath
import shutil
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator

from . import APP_NAME, __version__

REPO = "Pablokaer/StemSplitter"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
# STEMSPLITTER_UPDATE_API points the check at another server (tests use a local one)
API_LATEST = os.environ.get("STEMSPLITTER_UPDATE_API") or f"https://api.github.com/repos/{REPO}/releases/latest"
INDEX_FORMAT = 1
CHUNK = 1 << 20
GAP = 4 << 20  # unneeded archive bytes between two needed files still read in the same request (instead of a new one)
TIMEOUT = 30
RETRIES = 4
SELFTEST_TIMEOUT = 900  # the first start of new DLLs can be slow while the antivirus scans them
SPACE_MARGIN = 256 << 20

# the work folder next to the app (work_dir()): staged new files, the old files moved aside, the helper copy
WORK, NEW, OLD, HELPER = ".ss-update", "n", "o", "h"
PLAN, STATE, RESULT = "plan.json", "state.json", "result.json"

Progress = Callable[[float, str], None]


class UpdateError(Exception):
    """An update failure, with a message for the user."""


class Cancelled(UpdateError):
    def __init__(self):
        super().__init__("Cancelled")


# -- versions and releases ------------------------------------------------------------
def parse_version(text: str) -> tuple[int, ...]:
    """"v1.2.3" -> (1, 2, 3). Whatever follows the numbers ("-beta", "+cu130") is ignored."""
    nums = []
    for part in text.strip().lstrip("vV").split("."):
        digits = ""
        for ch in part:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        nums.append(int(digits))
        if len(digits) != len(part):
            break
    return tuple(nums)


def is_newer(remote: str, local: str = __version__) -> bool:
    r, loc = parse_version(remote), parse_version(local)
    if not r:
        return False
    n = max(len(r), len(loc))
    return r + (0,) * (n - len(r)) > loc + (0,) * (n - len(loc))


@dataclass
class Release:
    version: str  # "1.1.0"
    tag: str  # "v1.1.0"
    notes: str  # Markdown (the release description)
    page: str  # the release's web page
    assets: dict[str, tuple[str, int]] = field(default_factory=dict)  # file name -> (download URL, size)


def index_name(platform: str) -> str:
    return f"{APP_NAME}-{platform}.files.json"


def archive_name(platform: str) -> str:
    return f"{APP_NAME}-{platform}.zip"


def archive_parts(names: Iterable[str], archive: str) -> list[str]:
    """The archive's file names in order: the archive itself, or its parts `<archive>.001`, `.002`, …"""
    names = set(names)
    if archive in names:
        return [archive]
    parts = [n for n in names if n.startswith(archive + ".") and n[len(archive) + 1:].isdigit()]
    return sorted(parts, key=lambda n: int(n[len(archive) + 1:]))


# -- HTTP ----------------------------------------------------------------------------
def _check_url(url: str) -> None:
    """HTTPS only (plain HTTP is allowed to this computer, for the tests)."""
    u = urllib.parse.urlsplit(url)
    if u.scheme == "https" or (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost")):
        return
    raise UpdateError(f"Refusing to download over an insecure connection: {u.scheme}://{u.hostname}")


class _Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)  # GitHub sends downloads to a signed URL on another host
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = None
_opener_lock = threading.Lock()


def _ssl_context():
    import ssl

    if sys.platform.startswith("win"):
        return ssl.create_default_context()  # the Windows certificate store (also has company proxy roots)
    try:  # a frozen macOS app has no system CA bundle that OpenSSL can read: use certifi's (bundled for requests)
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _urlopen(url: str, headers: dict | None = None, timeout: float = TIMEOUT):
    global _opener
    _check_url(url)
    with _opener_lock:
        if _opener is None:
            _opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ssl_context()), _Redirects())
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{__version__}", **(headers or {})})
    return _opener.open(req, timeout=timeout)


def _reason(exc: BaseException) -> str:
    reason = getattr(exc, "reason", None)
    return str(reason or exc) or type(exc).__name__


def fetch_latest() -> Release | None:
    """The latest published release, or None when the repository has none yet."""
    try:
        with _urlopen(API_LATEST, {"Accept": "application/vnd.github+json"}) as r:
            data = json.load(r)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        if exc.code == 403:
            raise UpdateError("GitHub refused the request (too many checks from this network); try again later") from exc
        raise UpdateError(f"GitHub answered {exc.code} {exc.reason}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise UpdateError(f"Can't reach GitHub ({_reason(exc)})") from exc
    tag = str(data.get("tag_name") or "")
    assets = {a["name"]: (a["browser_download_url"], int(a["size"])) for a in data.get("assets", [])}
    return Release(version=tag.lstrip("vV"), tag=tag, notes=str(data.get("body") or ""),
                   page=str(data.get("html_url") or RELEASES_PAGE), assets=assets)


# -- the installed app ---------------------------------------------------------------
def build_info() -> dict:
    """What the release workflow wrote into the build (`build-info.json`: the platform name); {} otherwise."""
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return {}
    try:
        return json.loads((Path(base) / "build-info.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@dataclass
class Installation:
    root: Path  # the app's folder (Windows) or StemSplitter.app (macOS)
    exe: str  # the executable, relative to root ("/"-separated)
    platform: str  # "" for builds not made by the release workflow

    @classmethod
    def current(cls) -> Installation | None:
        """The packaged app that is running; None when running from source."""
        if not getattr(sys, "frozen", False):
            return None
        exe = Path(sys.executable).resolve()
        if sys.platform == "darwin" and exe.parent.name == "MacOS" and exe.parent.parent.name == "Contents":
            root = exe.parents[2]
        else:
            root = exe.parent
        return cls(root=root, exe=exe.relative_to(root).as_posix(), platform=str(build_info().get("platform", "")))

    @property
    def work(self) -> Path:
        return work_dir(self.root)


def work_dir(root: Path) -> Path:
    """Staging, backups and the helper live next to the app, on the same drive, so every move is a rename.

    The short names keep their paths as long as the app's own (`.ss-update/h` vs `StemSplitter`): on Windows a
    helper copy with longer paths could hit the 260-character limit when it loads its DLLs."""
    return root.parent / WORK


def _ext(p: Path) -> Path:
    r"""On Windows, the extended-length form of `p` (\\?\C:\...): the file operations then work past the
    260-character path limit, which a deep file in a folder with a long path can reach."""
    if os.name != "nt":
        return p
    s = str(p)
    if s.startswith("\\\\?\\"):
        return p
    s = os.path.abspath(s)
    return Path("\\\\?\\UNC\\" + s[2:] if s.startswith("\\\\") else "\\\\?\\" + s)


def _same_root(plan: dict, root: Path) -> bool:
    """Two copies of the app in one folder share the work folder: each only touches its own update."""
    try:
        return os.path.normcase(os.path.abspath(plan["root"])) == os.path.normcase(os.path.abspath(root))
    except (KeyError, TypeError):
        return False


def _writable(folder: Path) -> bool:
    probe = folder / f".{APP_NAME}-write-test-{os.getpid()}"
    try:
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def self_update_problem(inst: Installation | None, release: Release) -> str | None:
    """Why this copy can't install `release` by itself (the user then gets the download page), or None."""
    if inst is None:
        return "StemSplitter is running from source code here: update it with git pull."
    if not inst.platform:
        return "This build was not made by the release workflow, so it can't update itself."
    if index_name(inst.platform) not in release.assets or not archive_parts(release.assets,
                                                                            archive_name(inst.platform)):
        return f"Version {release.version} has no update files for this build ({inst.platform})."
    if not _writable(inst.root) or not _writable(inst.root.parent):
        return f"StemSplitter can't write to the folder it is installed in ({inst.root.parent})."
    return None


# -- file trees and indexes ----------------------------------------------------------
def _walk(root: Path) -> Iterator[tuple[str, Path, bool]]:
    """(relative path, path, is a symlink) for every file and symlink under root; symlinks are not followed."""
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        for name in list(dirnames):
            p = base / name
            if p.is_symlink():
                dirnames.remove(name)
                yield p.relative_to(root).as_posix(), p, True
        for name in filenames:
            p = base / name
            yield p.relative_to(root).as_posix(), p, p.is_symlink()


def _sha256(path: Path, on_bytes: Callable[[int], None] | None = None, cancel: threading.Event | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
            if on_bytes is not None:
                on_bytes(len(chunk))
            if cancel is not None and cancel.is_set():
                raise Cancelled()
    return h.hexdigest()


def make_index(root: Path, platform: str, version: str, archive: str) -> dict:
    """The file index of a build (written by CI next to the release archive)."""
    files: dict[str, dict] = {}
    name, root = root.name, _ext(root)
    for rel, p, link in sorted(_walk(root)):
        if link:
            files[rel] = {"link": os.readlink(p)}
            continue
        st = p.stat()
        meta: dict = {"size": st.st_size, "sha256": _sha256(p)}
        if os.name != "nt" and st.st_mode & 0o100:
            meta["exec"] = True
        files[rel] = meta
    return {"format": INDEX_FORMAT, "app": APP_NAME, "version": version, "platform": platform,
            "root": name, "archive": archive, "files": files}


def _safe_rel(rel: str) -> str:
    parts = rel.split("/")
    if not rel or rel.startswith("/") or "\\" in rel or ":" in rel or any(p in ("", ".", "..") for p in parts):
        raise UpdateError(f"Bad path in the update index: {rel!r}")
    return rel


def check_index(index: dict, platform: str | None = None, version: str | None = None) -> dict:
    """Reject an index this updater can't use, or one with paths that would leave the app's folder."""
    if index.get("format") != INDEX_FORMAT:
        raise UpdateError("This update needs a newer updater: download it from the release page.")
    if platform is not None and index.get("platform") != platform:
        raise UpdateError(f"The update index is for {index.get('platform')}, not {platform}.")
    if version is not None and index.get("version") != version:
        raise UpdateError(f"The update index is for version {index.get('version')}, not {version}.")
    files = index.get("files")
    if not isinstance(files, dict) or not files or not index.get("root"):
        raise UpdateError("The update index is empty or damaged.")
    for rel, meta in files.items():
        _safe_rel(rel)
        if "link" in meta:
            target = str(meta["link"])
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target))
            if target.startswith("/") or "\\" in target or resolved == ".." or resolved.startswith("../"):
                raise UpdateError(f"Bad link in the update index: {rel!r} -> {target!r}")
        elif not isinstance(meta.get("size"), int) or not isinstance(meta.get("sha256"), str):
            raise UpdateError(f"Bad entry in the update index: {rel!r}")
    return index


def fetch_index(release: Release, platform: str) -> dict:
    url, size = release.assets[index_name(platform)]
    try:
        with _urlopen(url) as r:
            data = json.loads(r.read(64 << 20))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise UpdateError(f"Can't download the update index ({_reason(exc)})") from exc
    return check_index(data, platform, release.version)


# -- plan ----------------------------------------------------------------------------
@dataclass
class Plan:
    version: str
    add: list[str]  # needed paths that don't exist in the installed app
    replace: list[str]  # needed paths that exist but differ
    remove: list[str]  # installed files the new version doesn't have (only inside the build's own folders)
    fetch: list[str]  # needed files that aren't staged yet: these are downloaded
    fetch_bytes: int  # their unpacked size

    @property
    def empty(self) -> bool:
        return not (self.add or self.replace or self.remove)


class _Throttle:
    """Calls `progress` at most ~10 times a second."""

    def __init__(self, progress: Progress | None, text: str, total: int):
        self.progress, self.text, self.total, self.done, self.last = progress, text, max(1, total), 0, 0.0

    def __call__(self, n: int) -> None:
        self.done += n
        now = time.monotonic()
        if self.progress is not None and now - self.last >= 0.1:
            self.last = now
            self.progress(min(1.0, self.done / self.total), self.text)


def _local_entries(root: Path) -> dict[str, tuple[str, object]]:
    entries: dict[str, tuple[str, object]] = {}
    if root.is_dir():
        for rel, p, link in _walk(root):
            entries[rel] = ("link", os.readlink(p)) if link else ("file", p.stat().st_size)
    return entries


def make_plan(root: Path, index: dict, staging: Path, progress: Progress | None = None,
              cancel: threading.Event | None = None) -> Plan:
    """Compare the installed app with the index: what to download, replace, add and remove."""
    root, staging = _ext(root), _ext(staging)
    files = index["files"]
    local = _local_entries(root)
    same_size = [rel for rel, m in files.items() if "link" not in m and local.get(rel) == ("file", m["size"])]
    tick = _Throttle(progress, "Checking the installed files…", sum(files[rel]["size"] for rel in same_size))
    same = {rel for rel in same_size if _sha256(root / rel, tick, cancel) == files[rel]["sha256"]}
    same |= {rel for rel, m in files.items() if "link" in m and local.get(rel) == ("link", m["link"])}
    need = [rel for rel in files if rel not in same]
    managed = {rel.split("/", 1)[0] for rel in files if "/" in rel}  # "_internal" or "Contents"
    remove = sorted(rel for rel in local if rel not in files and rel.split("/", 1)[0] in managed)
    fetch = []
    for rel in need:
        meta = files[rel]
        if "link" in meta:
            continue
        s = staging / rel
        if not (s.is_file() and not s.is_symlink() and s.stat().st_size == meta["size"]
                and _sha256(s, None, cancel) == meta["sha256"]):  # staged by an earlier, interrupted attempt
            fetch.append(rel)
    return Plan(version=str(index["version"]), add=[r for r in need if r not in local],
                replace=[r for r in need if r in local], remove=remove, fetch=fetch,
                fetch_bytes=sum(files[r]["size"] for r in fetch))


# -- reading the release archive -----------------------------------------------------
class Parts:
    """The bytes of an archive stored as one or more parts, read by offset."""

    def __init__(self, parts: list[tuple[str, int]], cancel: threading.Event | None = None):
        self.parts = parts  # (location, size)
        self.size = sum(size for _, size in parts)
        self.cancel = cancel

    def stream(self, start: int, end: int) -> Iterator[bytes]:
        """The bytes [start, end) in chunks, across part boundaries."""
        off = 0
        for loc, size in self.parts:
            a, b = max(start, off), min(end, off + size)
            if a < b:
                yield from self._part(loc, size, a - off, b - off)
            off += size

    def read(self, start: int, n: int) -> bytes:
        return b"".join(self.stream(start, min(start + n, self.size)))

    def _part(self, loc: str, size: int, a: int, b: int) -> Iterator[bytes]:
        raise NotImplementedError


class LocalParts(Parts):
    """Parts on disk (CI checks the release archive with the same code the app downloads it with)."""

    def _part(self, loc: str, size: int, a: int, b: int) -> Iterator[bytes]:
        with open(loc, "rb") as f:
            f.seek(a)
            while a < b:
                chunk = f.read(min(CHUNK, b - a))
                if not chunk:
                    raise UpdateError(f"{Path(loc).name} is shorter than expected")
                a += len(chunk)
                yield chunk


class RemoteParts(Parts):
    """Parts on a web server that answers range requests (GitHub release downloads do)."""

    def _part(self, loc: str, size: int, a: int, b: int) -> Iterator[bytes]:
        tries = 0
        while a < b:
            try:
                with _urlopen(loc, {"Range": f"bytes={a}-{b - 1}"}) as r:
                    if r.status != 206 and not (r.status == 200 and a == 0 and b == size):
                        raise UpdateError("The download server doesn't support partial downloads.")
                    while a < b:
                        chunk = r.read(min(CHUNK, b - a))
                        if not chunk:
                            raise ConnectionError("the connection closed early")
                        a += len(chunk)
                        tries = 0
                        yield chunk
            except (urllib.error.URLError, OSError) as exc:  # retried from where it stopped
                tries += 1
                if tries >= RETRIES:
                    raise UpdateError(f"The download failed ({_reason(exc)})") from exc
                for _ in range(tries * 20):
                    if self.cancel is not None and self.cancel.is_set():
                        raise Cancelled() from exc
                    time.sleep(0.1)


class _RangeFile:
    """Just enough of a seekable file for zipfile to read the central directory, fetched on demand."""

    TAIL = 1 << 20  # the first read fetches the last MB: the end records and, often, the whole directory

    def __init__(self, parts: Parts):
        self.parts, self.pos = parts, 0
        self.cache_start, self.cache = 0, b""

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = 0) -> int:
        base = {0: 0, 1: self.pos, 2: self.parts.size}[whence]
        self.pos = max(0, base + offset)
        return self.pos

    def read(self, n: int = -1) -> bytes:
        size = self.parts.size
        end = size if n is None or n < 0 else min(self.pos + n, size)
        if self.pos >= end:
            return b""
        if not (self.cache_start <= self.pos and end <= self.cache_start + len(self.cache)):
            start = min(self.pos, max(0, size - self.TAIL)) if not self.cache else self.pos
            stop = min(size, max(end, start + (256 << 10)))
            self.cache, self.cache_start = self.parts.read(start, stop - start), start
        out = self.cache[self.pos - self.cache_start:end - self.cache_start]
        self.pos = end
        return out

    def close(self) -> None:
        pass


class _Reader:
    """Reads a byte stream (archive offsets from `pos`) in exact amounts."""

    def __init__(self, chunks: Iterator[bytes], pos: int, on_bytes: Callable[[int], None],
                 cancel: threading.Event | None):
        self._it, self._buf, self.pos = chunks, b"", pos
        self._on_bytes, self._cancel = on_bytes, cancel

    def _fill(self) -> None:
        if self._cancel is not None and self._cancel.is_set():
            raise Cancelled()
        chunk = next(self._it, None)
        if chunk is None:
            raise UpdateError("The update archive ended early")
        self._on_bytes(len(chunk))
        self._buf += chunk

    def chunks(self, n: int) -> Iterator[bytes]:
        while n > 0:
            if not self._buf:
                self._fill()
            take = self._buf[:n]
            self._buf = self._buf[len(take):]
            self.pos += len(take)
            n -= len(take)
            yield take

    def read(self, n: int) -> bytes:
        return b"".join(self.chunks(n))

    def skip_to(self, offset: int) -> None:
        for _ in self.chunks(offset - self.pos):
            pass

    def close(self) -> None:
        close = getattr(self._it, "close", None)
        if close is not None:
            close()


def _members(parts: Parts) -> tuple[dict[str, zipfile.ZipInfo], list[int]]:
    """The archive's members by name, and the sorted start offsets (plus the directory's) that bound them."""
    try:
        with zipfile.ZipFile(_RangeFile(parts)) as zf:  # type: ignore[arg-type]
            infos = zf.infolist()
            cd_start = zf.start_dir
    except zipfile.BadZipFile as exc:
        raise UpdateError(f"The update archive is damaged ({exc})") from exc
    offsets = sorted({i.header_offset for i in infos} | {cd_start})
    return {i.filename.replace("\\", "/"): i for i in infos if not i.is_dir()}, offsets


def _extract(reader: _Reader, info: zipfile.ZipInfo, meta: dict, dest: Path, rel: str) -> None:
    head = reader.read(30)
    if head[:4] != b"PK\x03\x04":
        raise UpdateError("The update archive is damaged (bad file header)")
    name_len, extra_len = struct.unpack("<HH", head[26:30])
    reader.read(name_len + extra_len)
    if dest.is_dir() and not dest.is_symlink():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    h, size = hashlib.sha256(), 0
    inflate = zlib.decompressobj(-15) if info.compress_type == zipfile.ZIP_DEFLATED else None
    with open(tmp, "wb") as f:
        def put(data: bytes) -> None:
            nonlocal size
            f.write(data)
            h.update(data)
            size += len(data)

        for chunk in reader.chunks(info.compress_size):
            if inflate is None:
                put(chunk)
                continue
            put(inflate.decompress(chunk, 8 * CHUNK))  # bounded: a run of zeros inflates ~1000×
            while inflate.unconsumed_tail:
                put(inflate.decompress(inflate.unconsumed_tail, 8 * CHUNK))
        if inflate is not None:
            put(inflate.flush())
    if size != meta["size"] or h.hexdigest() != meta["sha256"]:
        tmp.unlink()
        raise UpdateError(f"{rel} doesn't match the update index (damaged download)")
    if meta.get("exec") and os.name != "nt":
        os.chmod(tmp, 0o755)
    os.replace(tmp, dest)


def download(parts: Parts, index: dict, plan: Plan, staging: Path, progress: Progress | None = None,
             cancel: threading.Event | None = None) -> None:
    """Stage every file of the plan in `staging`: the files to fetch from the archive, the symlinks from the index."""
    staging = _ext(staging)
    files = index["files"]
    for rel in plan.add + plan.replace:
        if "link" in files[rel]:
            s = staging / rel
            s.parent.mkdir(parents=True, exist_ok=True)
            if os.path.lexists(s):
                s.unlink() if s.is_symlink() or not s.is_dir() else shutil.rmtree(s)
            os.symlink(files[rel]["link"], s)
    if not plan.fetch:
        return
    members, offsets = _members(parts)
    prefix = str(index["root"]) + "/"
    jobs = []
    for rel in plan.fetch:
        info = members.get(prefix + rel)
        if info is None:
            raise UpdateError(f"{rel} is missing from the update archive")
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) or info.flag_bits & 0x1:
            raise UpdateError(f"{rel} is stored in a way the updater can't read")
        end = offsets[bisect.bisect_right(offsets, info.header_offset)]
        jobs.append((info.header_offset, end, rel, info))
    jobs.sort(key=lambda j: j[0])
    groups: list[list] = []  # nearby files are read with one request
    for job in jobs:
        if groups and job[0] - groups[-1][-1][1] <= GAP:
            groups[-1].append(job)
        else:
            groups.append([job])
    total = sum(g[-1][1] - g[0][0] for g in groups)
    tick = _Throttle(progress, f"Downloading {_mb(total)}…", total)
    for group in groups:
        reader = _Reader(parts.stream(group[0][0], group[-1][1]), group[0][0], tick, cancel)
        try:
            for start, _end, rel, info in group:
                reader.skip_to(start)
                _extract(reader, info, files[rel], staging / rel, rel)
        finally:
            reader.close()


def _mb(n: int) -> str:
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{max(n / 1e6, 0.1):.1f} MB"


def remote_parts(release: Release, platform: str, cancel: threading.Event | None = None) -> RemoteParts:
    names = archive_parts(release.assets, archive_name(platform))
    return RemoteParts([release.assets[n] for n in names], cancel)


def local_parts(folder: Path, archive: str) -> LocalParts:
    names = archive_parts((p.name for p in folder.iterdir()), archive)
    if not names:
        raise UpdateError(f"No {archive} (or {archive}.001, …) in {folder}")
    return LocalParts([(str(folder / n), (folder / n).stat().st_size) for n in names])


# -- installing ----------------------------------------------------------------------
def _depth(rel: str) -> int:
    return rel.count("/")


def _move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)


def _remove_empty_dirs(folder: Path) -> None:
    """Remove `folder` if it holds nothing but empty folders (OSError otherwise)."""
    for dirpath, _dirs, _files in os.walk(folder, topdown=False):
        os.rmdir(dirpath)


def apply(root: Path, staging: Path, backup: Path, plan: dict) -> None:
    """Move the old files to `backup` and the staged ones into the app. Undo it with rollback()."""
    root, staging, backup = _ext(root), _ext(staging), _ext(backup)
    for rel in sorted(plan["remove"], key=_depth, reverse=True):
        _move(root / rel, backup / rel)
    for rel in plan["replace"]:  # one file at a time: the app is never left without its executable
        _move(root / rel, backup / rel)
        _move(staging / rel, root / rel)
    for rel in sorted(plan["add"], key=_depth):
        dst = root / rel
        if dst.is_dir() and not dst.is_symlink():  # an old folder where the new version has a file
            _remove_empty_dirs(dst)
        _move(staging / rel, dst)


def rollback(root: Path, staging: Path, backup: Path, plan: dict) -> None:
    """Undo apply(), also a partial one (after a crash): every step looks at what is on disk."""
    root, staging, backup = _ext(root), _ext(staging), _ext(backup)
    for rel in sorted(plan["add"] + plan["replace"], key=_depth, reverse=True):
        dst, src = root / rel, staging / rel
        if os.path.lexists(dst) and not os.path.lexists(src) and not (dst.is_dir() and not dst.is_symlink()):
            _move(dst, src)
    for rel in sorted(plan["remove"] + plan["replace"], key=_depth):
        old = backup / rel
        if os.path.lexists(old):
            dst = root / rel
            if dst.is_dir() and not dst.is_symlink():  # a folder created for the new version's files
                _remove_empty_dirs(dst)
            _move(old, dst)


def clone_tree(src: Path, dst: Path) -> None:
    """A copy of the app made of hard links: no data is copied (plain copies where the drive has no links)."""
    src, dst = _ext(src), _ext(dst)
    if dst.exists():
        shutil.rmtree(dst)
    for rel, p, link in _walk(src):
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if link:
            os.symlink(os.readlink(p), target)
            continue
        try:
            os.link(p, target)
        except OSError:
            shutil.copy2(p, target)


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        import psutil

        return psutil.pid_exists(int(pid))
    except ImportError:
        return False


def prepare(inst: Installation, release: Release, progress: Progress | None = None,
            cancel: threading.Event | None = None) -> Plan:
    """Steps 2 and 3: work out what changed and download it into the staging folder (run on a thread)."""
    work = inst.work
    staging = work / NEW
    if progress:
        progress(0.0, "Reading the update index…")
    index = fetch_index(release, inst.platform)
    plan = make_plan(inst.root, index, staging, progress, cancel)
    if plan.empty:
        return plan
    work.mkdir(exist_ok=True)
    free = shutil.disk_usage(work).free
    if free < plan.fetch_bytes + SPACE_MARGIN:
        raise UpdateError(f"Not enough free disk space: the update needs {_mb(plan.fetch_bytes + SPACE_MARGIN)} "
                          f"next to the app and there is {_mb(free)}.")
    download(remote_parts(release, inst.platform, cancel), index, plan, staging, progress, cancel)
    return plan


def start_install(inst: Installation, plan: Plan, progress: Progress | None = None) -> None:
    """Step 4, in the app: write the plan, start the helper copy, and let the caller quit right away."""
    work = inst.work
    for leftover in (work / OLD, work / STATE, work / RESULT):  # from an attempt that never ran
        if leftover.is_dir():
            shutil.rmtree(_ext(leftover))
        elif leftover.exists():
            leftover.unlink()
    if progress:
        progress(1.0, "Preparing the installer…")
    # on macOS the copy keeps the bundle's name (StemSplitter.app); paths there have no 260-character limit
    helper = work / HELPER / inst.root.name if sys.platform == "darwin" else work / HELPER
    clone_tree(inst.root, helper)
    _write_json(work / PLAN, {"root": str(inst.root), "exe": inst.exe, "version": plan.version,
                              "from": __version__, "pid": os.getpid(), "add": plan.add,
                              "replace": plan.replace, "remove": plan.remove})
    args = [str(helper / inst.exe), "--apply-update", str(work)]
    if sys.platform.startswith("win"):
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        subprocess.Popen(args, cwd=helper, creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(args, cwd=helper, start_new_session=True, close_fds=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_selftest(exe: Path, timeout: float = SELFTEST_TIMEOUT) -> tuple[bool, str]:
    from .platform_utils import app_data_dir, subprocess_flags

    report = app_data_dir() / "selftest.txt"
    try:
        report.unlink()
    except OSError:
        pass
    try:
        code = subprocess.run([str(exe), "--selftest"], cwd=exe.parent, timeout=timeout, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **subprocess_flags()).returncode
    except subprocess.TimeoutExpired:
        return False, f"The self-test didn't finish in {timeout // 60:.0f} minutes."
    except OSError as exc:
        return False, f"The new version didn't start ({exc})."
    try:
        text = report.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        text = ""
    return code == 0, text or f"exit code {code}"


def launch(root: Path, exe: str) -> None:
    """Start the app detached from this process."""
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-n", str(root)])
        return
    args = [str(root / exe)]
    if sys.platform.startswith("win"):
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        subprocess.Popen(args, cwd=root, creationflags=flags, close_fds=True)
    else:
        subprocess.Popen(args, cwd=root, start_new_session=True, close_fds=True)


def install(work: Path, status: Callable[[str], None] = lambda text: None,
            selftest: Callable[[Path], tuple[bool, str]] = run_selftest) -> dict:
    """The helper's job (`--apply-update`): replace the files, check the new version, undo it if anything fails.

    Returns the result that the app shows when it starts again (also saved as result.json)."""
    plan = _read_json(work / PLAN)
    root, staging, backup = Path(plan["root"]), work / NEW, work / OLD
    result = {"ok": False, "version": plan["version"], "from": plan["from"]}
    status("Waiting for StemSplitter to close…")
    deadline = time.monotonic() + 120
    while _pid_alive(plan.get("pid")) and time.monotonic() < deadline:
        time.sleep(0.2)
    if _pid_alive(plan.get("pid")):
        result["message"] = "StemSplitter didn't close, so nothing was changed."
        _write_json(work / RESULT, result)
        return result
    _write_json(work / STATE, {"state": "applying", "helper": os.getpid()})
    try:
        status(f"Installing version {plan['version']}…")
        apply(root, staging, backup, plan)
        status("Checking the new version…")
        ok, report = selftest(root / plan["exe"])
        if ok:
            result.update(ok=True, message="")
        else:
            result["message"] = "The new version failed its self-test, so the previous version was kept.\n\n" + \
                                report[-3000:]
    except OSError as exc:
        result["message"] = f"The app's files could not be replaced ({exc}), so the previous version was kept."
    if not result["ok"]:
        status("Restoring the previous version…")
        try:
            rollback(root, staging, backup, plan)
        except OSError as exc:
            result["message"] += (f"\n\nThe previous version could not be restored ({exc}). A complete copy of it "
                                  f"is in {work / HELPER}.")
            _write_json(work / RESULT, result)
            _write_json(work / STATE, {"state": "broken"})
            return result
    _write_json(work / RESULT, result)
    _write_json(work / STATE, {"state": "done"})
    return result


def recover(root: Path) -> dict | None:
    """At start-up: undo an install that was interrupted (power cut, crash), unless its helper still runs."""
    work = work_dir(root)
    state = _read_json(work / STATE)
    if state.get("state") != "applying" or _pid_alive(state.get("helper")):
        return None
    plan = _read_json(work / PLAN)
    if not _same_root(plan, root):
        return None
    result = {"ok": False, "version": plan.get("version", "?"), "from": plan.get("from", __version__),
              "message": "The update was interrupted, so the previous version was restored."}
    try:
        rollback(root, work / NEW, work / OLD, plan)
    except (OSError, KeyError) as exc:
        result["message"] = f"The update was interrupted and could not be undone ({exc})."
        _write_json(work / STATE, {"state": "broken"})
    else:
        _write_json(work / STATE, {"state": "done"})
    _write_json(work / RESULT, result)
    return result


def take_result(root: Path) -> dict | None:
    """At start-up: the result of the update that just ran (shown once), and clean up its work folder.

    Without a result only the staged downloads are kept, so a cancelled update continues where it stopped."""
    work = work_dir(root)
    if not work.is_dir():
        return None
    plan = _read_json(work / PLAN)
    if plan and not _same_root(plan, root):
        return None
    state = _read_json(work / STATE)
    if state.get("state") == "applying" or (state.get("state") == "broken" and not (work / RESULT).exists()):
        return None
    result = _read_json(work / RESULT) or None
    if result is not None and state.get("state") != "broken":
        targets = [work]
    else:
        targets = [work / HELPER, work / OLD, work / PLAN, work / STATE, work / RESULT]
        if result is not None:  # broken: keep the backups and the helper copy for the user
            targets = [work / RESULT]

    def clean():  # the helper may take a moment to exit and release its files
        for _ in range(60):
            left = [t for t in targets if os.path.lexists(t)]
            for t in left:
                try:
                    shutil.rmtree(_ext(t)) if t.is_dir() and not t.is_symlink() else t.unlink()
                except OSError:
                    pass
            if not any(os.path.lexists(t) for t in targets):
                return
            time.sleep(1)

    threading.Thread(target=clean, name="StemSplitter update cleanup", daemon=True).start()
    return result
