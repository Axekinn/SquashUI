"""Tool managers: find, download and update chdman, DolphinTool, maxcso, the PKG extractors and the rest."""
from __future__ import annotations

import contextlib
import hashlib
import json
import mmap
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .constants import APP_DIR, APP_NAME, SUBPROCESS_FLAGS, TOOLS_DIR_NAME
from .system import _py7zr, find_7zip, portable_path, resolve_dolphintool


@dataclass(frozen=True, slots=True)
class Release:
    tag: str
    name: str
    url: str
    size: int
    sha256: str | None


def mame_version(tag: str) -> str:
    m = re.fullmatch(r"mame0(\d+)", tag)
    return f"0.{m[1]}" if m else tag


class ReleaseUnavailable(RuntimeError):
    """A tool has no prebuilt download (the message tells the user how to get it)."""


def migrate_legacy_tool_folder(old: Path, new: Path) -> None:
    """Move a pre-v8.5 tool folder (./chdman_tools ...) to its place under ./tools, keeping everything already
    downloaded. Never overwrites, and a failure just leaves the old folder where it was."""
    if not old.is_dir() or old.resolve() == new.resolve():
        return
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
        if not new.exists():
            os.replace(old, new)
            return
        for child in list(old.iterdir()):  # the new folder already exists: bring over whatever is missing from it
            if not (new / child.name).exists():
                shutil.move(str(child), str(new / child.name))
        with contextlib.suppress(OSError):
            old.rmdir()  # only if nothing is left
    except OSError:
        pass


class ToolManager:
    """Keeps a private, auto-updated copy of a command-line tool next to the app (Windows only).

    Subclasses say where the tool comes from (class attributes + _latest_release), how to unpack a
    download (_unpack) and how to tell it works (_probe). Everything else is shared: the tool folder,
    the version cache, the update check, the download, and the rollback if a new copy doesn't run.
    """

    # event prefix, tool name, origin, its folder under ./tools, its pre-v8.5 folder, release-tag regex, exe name
    KEY = NAME = SOURCE = SUBDIR = LEGACY_FOLDER = TAG_RE = EXE_NAME = ""
    USER_AGENT = f"{APP_NAME}-updater"

    def __init__(self, emit: Callable[[tuple], None]):
        self._emit = emit
        self.root = self._pick_root()

    @classmethod
    def _pick_root(cls) -> Path:
        """./tools/<tool> beside the app, or under %LOCALAPPDATA% when the app folder isn't writable. A folder from
        an older version (./chdman_tools ...) is moved in the first time, so nothing downloaded is lost."""
        bases = [APP_DIR]
        if local := os.environ.get("LOCALAPPDATA"):
            bases.append(Path(local) / APP_NAME)
        for base in bases:
            target = base / TOOLS_DIR_NAME / cls.SUBDIR
            if cls.LEGACY_FOLDER:
                migrate_legacy_tool_folder(base / cls.LEGACY_FOLDER, target)
            try:
                target.mkdir(parents=True, exist_ok=True)
                probe = target / ".write_test"
                probe.write_text("x")
                probe.unlink()
                return target
            except OSError:
                continue
        return Path(tempfile.gettempdir()) / f"{APP_NAME.lower()}_{cls.SUBDIR}"

    def _label(self, tag: str) -> str:
        return tag

    def _status(self, text: str) -> None:
        self._emit((f"{self.KEY}_status", text))

    def _shown(self, exe: Path) -> str:
        """How a path appears in the tool box: ./<tool folder>/... rather than the full absolute path."""
        return portable_path(exe)

    def cached(self) -> tuple[str, Path] | None:
        """(release tag, exe) of the copy this app downloaded last, if it is still on disk."""
        try:
            tag = (self.root / "current.txt").read_text().strip()
        except OSError:
            return None
        if not re.fullmatch(self.TAG_RE, tag):
            return None
        folder = self.root / tag
        exe = next(folder.glob(self.EXE_NAME), None) or next(folder.glob(f"*/{self.EXE_NAME}"), None)
        return (tag, exe) if exe else None

    def _local_copy(self) -> Path | None:
        """A copy of the tool the user placed in the tool folder themselves (chdman has none)."""
        return None

    def run(self) -> None:
        """Background thread entry point. Always ends with a <key>_ready event."""
        try:
            self._run()
        except Exception as exc:
            self._status(f"{self.NAME} update failed: {exc}")
        finally:
            self._emit((f"{self.KEY}_ready",))

    def _run(self) -> None:
        if local := self._local_copy():
            self._emit((f"{self.KEY}_path", self._shown(local)))
            try:
                self._probe(local)
                note = ""
            except Exception as exc:
                note = f" - WARNING: {exc}"
            self._status(f"{self.NAME} found in {portable_path(local.parent)} - using your copy, "
                         f"auto-update skipped{note}")
            return
        cached = self.cached()
        if cached:
            self._emit((f"{self.KEY}_path", self._shown(cached[1])))
            self._status(f"{self.NAME} from {self.SOURCE} {self._label(cached[0])} - checking for updates...")
        if sys.platform != "win32":
            if not cached:
                self._status(f"Auto-download is Windows-only: install {self.NAME} with your package manager "
                             "(or select it manually).")
            return
        try:
            release = self._latest_release()
        except ReleaseUnavailable as exc:
            self._status(str(exc))
            return
        except Exception:
            self._status(f"Offline - using the downloaded {self.NAME}." if cached
                         else f"Couldn't reach the download server - select {self.NAME} manually.")
            return
        if cached and cached[0] == release.tag:
            self._status(f"{self.NAME} from {self.SOURCE} {self._label(release.tag)} (up to date)")
            return
        self._install(release)

    # ---- what a subclass provides ----------------------------------------------------------------
    def _latest_release(self) -> Release:
        raise NotImplementedError

    def _unpack(self, archive: Path, tmp: Path, dest: Path) -> None:
        """Leave a working tool at dest / EXE_NAME (tmp is scratch space that is deleted afterwards)."""
        raise NotImplementedError

    @staticmethod
    def _probe(exe: Path) -> str:
        """Run the binary once; raise if it doesn't behave like the real tool."""
        raise NotImplementedError

    def _extract_all(self, archive: Path, dest: Path) -> None:
        """Unpack a whole .7z / .zip package into dest."""
        if archive.suffix.lower() == ".zip":
            with zipfile.ZipFile(archive) as z:
                z.extractall(dest)
            return
        if seven := find_7zip():
            rc = subprocess.run(
                [seven, "x", "-y", f"-o{dest}", str(archive)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=SUBPROCESS_FLAGS,
            ).returncode
            if rc in (0, 1):
                return
        with _py7zr().SevenZipFile(archive) as z:
            z.extractall(path=dest)

    # ---- shared download / install ---------------------------------------------------------------
    def _download(self, rel: Release, dest: Path) -> None:
        req = urllib.request.Request(rel.url, headers={"User-Agent": self.USER_AGENT})
        sha, done, last = hashlib.sha256(), 0, -1
        with urllib.request.urlopen(req, timeout=30) as resp, dest.open("wb") as out:
            total = int(resp.headers.get("Content-Length") or rel.size or 0)
            while chunk := resp.read(1 << 20):
                out.write(chunk)
                sha.update(chunk)
                done += len(chunk)
                if (pct := done * 100 // total if total else done >> 20) != last:
                    last = pct
                    size = f"{done >> 20} / {total >> 20} MB" if total else f"{done >> 20} MB"
                    self._status(f"Downloading {self.SOURCE} {self._label(rel.tag)} for {self.NAME}... {size}")
        if rel.size and done != rel.size:
            raise RuntimeError("download was incomplete")
        if rel.sha256 and sha.hexdigest().lower() != rel.sha256.lower():
            raise RuntimeError("download checksum mismatch")

    def _prune(self, keep: str) -> None:
        for old in self.root.iterdir():  # old versions may be in use; failures are retried next launch
            if old.is_dir() and re.fullmatch(self.TAG_RE, old.name) and old.name != keep:
                shutil.rmtree(old, ignore_errors=True)

    def _install(self, rel: Release) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="dl_", dir=self.root))
        try:
            archive = tmp / rel.name
            self._download(rel, archive)
            self._status(f"Extracting {self.NAME} from {self.SOURCE} {self._label(rel.tag)}...")
            dest = self.root / rel.tag
            self._unpack(archive, tmp, dest)
            try:
                self._probe(dest / self.EXE_NAME)
            except Exception:  # a copy that doesn't run is removed, so the previous good version stays active
                shutil.rmtree(dest, ignore_errors=True)
                raise
            (self.root / "current.txt").write_text(rel.tag)
            self._emit((f"{self.KEY}_path", self._shown(dest / self.EXE_NAME)))
            self._status(f"{self.NAME} from {self.SOURCE} {self._label(rel.tag)} (just updated)")
            self._prune(rel.tag)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ChdmanManager(ToolManager):
    """chdman comes from the official MAME release: one small GitHub API call per launch, and a download
    (~85 MB, the full MAME package) only when MAME has published a newer release."""

    KEY, NAME, SOURCE = "chdman", "chdman", "MAME"
    SUBDIR, LEGACY_FOLDER, TAG_RE, EXE_NAME = "chdman", "chdman_tools", r"mame\d+", "chdman.exe"
    API_URL = "https://api.github.com/repos/mamedev/mame/releases/latest"
    SFX_SIGNATURE = b"7z\xbc\xaf\x27\x1c"

    def _label(self, tag: str) -> str:
        return mame_version(tag)

    def _latest_release(self) -> Release:
        headers = {"User-Agent": self.USER_AGENT, "Accept": "application/vnd.github+json"}
        with urllib.request.urlopen(urllib.request.Request(self.API_URL, headers=headers), timeout=10) as resp:
            data = json.load(resp)
        tag = data["tag_name"]
        if not re.fullmatch(self.TAG_RE, tag):
            raise RuntimeError(f"unexpected release tag {tag!r}")
        arch = ("arm64",) if platform.machine().lower() in ("arm64", "aarch64") else ("x64", "64bit")
        pattern = re.compile(rf"mame\d+b_(?:{'|'.join(arch)})\.exe", re.IGNORECASE)
        for asset in data["assets"]:
            if pattern.fullmatch(asset["name"]):
                digest = asset.get("digest") or ""
                sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else None
                return Release(tag, asset["name"], asset["browser_download_url"], asset.get("size", 0), sha)
        raise RuntimeError("no Windows binary found in the latest MAME release")

    def _extract_chdman(self, sfx: Path, out_dir: Path) -> Path:
        """Pull just chdman.exe out of MAME's self-extracting 7-Zip package."""
        target = out_dir / "chdman.exe"
        if seven := find_7zip():
            rc = subprocess.run(
                [seven, "e", "-y", "-r", f"-o{out_dir}", str(sfx), "chdman.exe"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=SUBPROCESS_FLAGS,
            ).returncode
            if rc in (0, 1) and target.is_file():
                return target
        # Pure-Python path: an SFX is just an exe stub followed by an ordinary .7z archive.
        py7zr = _py7zr()
        blob = out_dir / "payload.7z"
        with sfx.open("rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            offset = mm.find(self.SFX_SIGNATURE)
            while offset != -1:
                blob.write_bytes(mm[offset:])
                try:
                    with py7zr.SevenZipFile(blob) as z:
                        names = [n for n in z.getnames() if n.lower().rsplit("/", 1)[-1] == "chdman.exe"]
                    if names:
                        with py7zr.SevenZipFile(blob) as z:
                            z.extract(path=out_dir, targets=names[:1])
                        found = out_dir / names[0]
                        if found.is_file():
                            return found
                except Exception:
                    pass
                offset = mm.find(self.SFX_SIGNATURE, offset + 1)
        raise RuntimeError("could not extract chdman.exe from the MAME package")

    def _unpack(self, archive: Path, tmp: Path, dest: Path) -> None:
        extracted = self._extract_chdman(archive, tmp)
        dest.mkdir(exist_ok=True)
        os.replace(extracted, dest / self.EXE_NAME)

    @staticmethod
    def _probe(exe: Path) -> str:
        """chdman with no arguments just prints its usage."""
        res = subprocess.run([str(exe)], capture_output=True, timeout=30, stdin=subprocess.DEVNULL,
                             creationflags=SUBPROCESS_FLAGS)
        text = (res.stdout + res.stderr).decode(errors="replace")
        if "chdman" not in text.lower():
            raise RuntimeError("the downloaded chdman did not run")
        return text


class DolphinToolManager(ToolManager):
    """The same idea for DolphinTool, taken from the newest official Dolphin release (Windows only).

    The download page lists no checksums, so this copy is protected by HTTPS plus a test run of the
    tool before it is used. Dolphin's 7z is extracted in full so DolphinTool keeps any files beside it.
    A DolphinTool the user dropped into ./tools/dolphintool by hand is used as is (see _local_copy).
    """

    KEY, NAME, SOURCE = "dolphin", "DolphinTool", "Dolphin"
    SUBDIR, LEGACY_FOLDER, TAG_RE, EXE_NAME = "dolphintool", "dolphin_tools", r"\d{4}[a-z]?", "DolphinTool.exe"
    PAGE_URL = "https://dolphin-emu.org/download/?nocr=true"

    def _local_copy(self) -> Path | None:
        """DolphinTool dropped into ./tools/dolphintool by hand: directly in it, or inside an unzipped Dolphin
        folder (up to two levels down). Folders the updater manages (release tags, dl_* temp) are skipped,
        so a copy the user placed always wins over an auto-downloaded one."""
        def unmanaged(d: Path) -> bool:
            return d.is_dir() and not d.name.startswith("dl_") and not re.fullmatch(self.TAG_RE, d.name)

        try:
            level1 = sorted(d for d in self.root.iterdir() if unmanaged(d))
            level2 = sorted(g for d in level1 for g in d.iterdir() if unmanaged(g))
        except OSError:
            level1 = level2 = []
        for folder in (self.root, *level1, *level2):
            if found := resolve_dolphintool(str(folder)):
                return Path(found)
        return None

    def _latest_release(self) -> Release:
        req = urllib.request.Request(self.PAGE_URL, headers={"User-Agent": self.USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", "replace")
        arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
        # The releases table comes before the development builds and lists the newest release first.
        m = re.search(rf"https://dl\.dolphin-emu\.org/releases/(\d{{4}}[a-z]?)/dolphin-\1-{arch}\.7z",
                      html, re.IGNORECASE)
        if not m:
            raise RuntimeError("no Windows download found on Dolphin's download page")
        return Release(m[1], f"dolphin-{m[1]}-{arch}.7z", m[0], 0, None)

    def _unpack(self, archive: Path, tmp: Path, dest: Path) -> None:
        staging = tmp / "extracted"
        staging.mkdir()
        self._extract_all(archive, staging)
        found = next(staging.rglob(self.EXE_NAME), None)
        if not found:
            raise RuntimeError("DolphinTool.exe is not in this Dolphin package")
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(found.parent, dest)  # keep the whole folder: DolphinTool may need files beside it

    @staticmethod
    def _probe(exe: Path) -> str:
        res = subprocess.run([str(exe), "convert", "-h"], capture_output=True, timeout=30,
                             stdin=subprocess.DEVNULL, creationflags=SUBPROCESS_FLAGS)
        text = (res.stdout + res.stderr).decode(errors="replace")
        if "convert" not in text.lower():
            raise RuntimeError("DolphinTool did not run (is the 64-bit Visual C++ redistributable installed?)")
        return text


class MaxCsoManager(ToolManager):
    """maxcso (PSP ISO -> CSO) from the newest release of its GitHub project (Windows only).

    The Windows package ships maxcso.exe (64-bit) beside maxcso32.exe; the 64-bit one is used. A maxcso
    dropped into ./tools/maxcso by hand is used as is (see _local_copy).
    """

    KEY, NAME, SOURCE = "maxcso", "maxcso", "maxcso"
    SUBDIR, LEGACY_FOLDER, TAG_RE, EXE_NAME = "maxcso", "maxcso_tools", r"v?\d+(?:\.\d+)+", "maxcso.exe"
    API_URL = "https://api.github.com/repos/unknownbrackets/maxcso/releases/latest"

    def _local_copy(self) -> Path | None:
        def unmanaged(d: Path) -> bool:
            return d.is_dir() and not d.name.startswith("dl_") and not re.fullmatch(self.TAG_RE, d.name)

        try:
            folders = [self.root, *sorted(d for d in self.root.iterdir() if unmanaged(d))]
        except OSError:
            folders = [self.root]
        for folder in folders:
            for name in ("maxcso.exe", "maxcso"):
                if (folder / name).is_file():
                    return folder / name
        return None

    def _latest_release(self) -> Release:
        headers = {"User-Agent": self.USER_AGENT, "Accept": "application/vnd.github+json"}
        with urllib.request.urlopen(urllib.request.Request(self.API_URL, headers=headers), timeout=10) as resp:
            data = json.load(resp)
        tag = data["tag_name"]
        if not re.fullmatch(self.TAG_RE, tag):
            raise RuntimeError(f"unexpected release tag {tag!r}")
        packages = [a for a in data["assets"] if a["name"].lower().endswith((".7z", ".zip"))]
        # Prefer the package that says it is for Windows; a lone package is taken as is.
        windows = [a for a in packages if "win" in a["name"].lower()] or (packages if len(packages) == 1 else [])
        if not windows:
            raise RuntimeError("no Windows package found in the latest maxcso release")
        asset = windows[0]
        digest = asset.get("digest") or ""
        sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else None
        return Release(tag, asset["name"], asset["browser_download_url"], asset.get("size", 0), sha)

    def _unpack(self, archive: Path, tmp: Path, dest: Path) -> None:
        staging = tmp / "extracted"
        staging.mkdir()
        self._extract_all(archive, staging)
        found = next((p for p in staging.rglob("*") if p.is_file() and p.name.lower() == self.EXE_NAME), None)
        if not found:
            raise RuntimeError("maxcso.exe is not in this maxcso package")
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(found.parent, dest)  # keep the whole folder, in case anything sits beside the exe

    @staticmethod
    def _probe(exe: Path) -> str:
        res = subprocess.run([str(exe), "--help"], capture_output=True, timeout=30,
                             stdin=subprocess.DEVNULL, creationflags=SUBPROCESS_FLAGS)
        text = (res.stdout + res.stderr).decode(errors="replace")
        if "maxcso" not in text.lower():
            raise RuntimeError("the downloaded maxcso did not run")
        return text


class PkgExtractorManager(ToolManager):
    """ShadPS4Plus PKG_Extractor (the PKG code that used to live in shadPS4, as a standalone command-line tool)
    from the newest "PKG Extractor" release of AzaharPlus/shadPS4Plus (Windows only).

    That repository also publishes emulator builds, so the release list is searched for the newest tag that
    starts with PKG_EXTRACTOR_ instead of trusting /releases/latest. The release holds the extractor, an
    AppImage and install scripts: only the Windows program is taken. A copy dropped into ./tools/pkg-extractor
    by hand is used as is. The tool is never started just to test it.
    """

    KEY, NAME, SOURCE = "shadplus", "ShadPS4Plus PKG_Extractor", "AzaharPlus/shadPS4Plus"
    SUBDIR, LEGACY_FOLDER, TAG_RE, EXE_NAME = "pkg-extractor", "", r"PKG_EXTRACTOR_\d+(?:_\d+)+", "pkg_extractor.exe"
    API_URL = "https://api.github.com/repos/AzaharPlus/shadPS4Plus/releases?per_page=30"

    def _label(self, tag: str) -> str:
        return "v" + tag.removeprefix("PKG_EXTRACTOR_").replace("_", ".")

    def _local_copy(self) -> Path | None:
        def unmanaged(d: Path) -> bool:
            return d.is_dir() and not d.name.startswith("dl_") and not re.fullmatch(self.TAG_RE, d.name)

        try:
            folders = [self.root, *sorted(d for d in self.root.iterdir() if unmanaged(d))]
        except OSError:
            folders = [self.root]
        for folder in folders:
            for name in ("pkg_extractor.exe", "pkg_extractor"):
                if (folder / name).is_file():
                    return folder / name
        return None

    @staticmethod
    def _windows_asset(assets: list[dict]) -> dict | None:
        """The Windows download: a lone pkg_extractor .exe, else a .zip / .7z that isn't the Linux package."""
        def usable(a: dict, suffixes: tuple[str, ...]) -> bool:
            n = a["name"].lower()
            return n.endswith(suffixes) and not any(w in n for w in ("linux", "appimage", "install_pkg"))

        exes = [a for a in assets if usable(a, (".exe",)) and "extract" in a["name"].lower()]
        packs = [a for a in assets if usable(a, (".zip", ".7z"))]
        packs.sort(key=lambda a: "win" not in a["name"].lower())  # a "win" package first
        return (exes or packs or [None])[0]

    def _latest_release(self) -> Release:
        headers = {"User-Agent": self.USER_AGENT, "Accept": "application/vnd.github+json"}
        with urllib.request.urlopen(urllib.request.Request(self.API_URL, headers=headers), timeout=10) as resp:
            releases = json.load(resp)
        for data in releases:
            if data.get("draft") or data.get("prerelease") or not re.fullmatch(self.TAG_RE, data.get("tag_name", "")):
                continue
            if asset := self._windows_asset(data.get("assets", [])):
                digest = asset.get("digest") or ""
                sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else None
                return Release(data["tag_name"], asset["name"], asset["browser_download_url"], asset.get("size", 0), sha)
        raise RuntimeError("no Windows PKG Extractor package found in the shadPS4Plus releases")

    def _unpack(self, archive: Path, tmp: Path, dest: Path) -> None:
        if archive.suffix.lower() == ".exe":  # the release is the bare program
            staging = tmp / "extracted"
            staging.mkdir()
            found = staging / self.EXE_NAME
            shutil.copy2(archive, found)
        else:
            staging = tmp / "extracted"
            staging.mkdir()
            self._extract_all(archive, staging)
            exes = [p for p in staging.rglob("*") if p.is_file() and p.suffix.lower() == ".exe"]
            found = (next((p for p in exes if p.name.lower() == self.EXE_NAME), None)
                     or next((p for p in exes if "extractor" in p.name.lower()), None))
            if not found:
                raise RuntimeError("pkg_extractor.exe is not in this PKG Extractor package")
            if found.name != self.EXE_NAME:  # the app looks for one fixed name
                target = found.with_name(self.EXE_NAME)
                if found.name.lower() == self.EXE_NAME:
                    found.rename(target)  # only the casing differs
                else:
                    shutil.copy2(found, target)
                found = target
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(found.parent, dest)  # keep the whole folder: the tool may need DLLs beside it

    @staticmethod
    def _probe(exe: Path) -> str:
        with exe.open("rb") as f:
            magic = f.read(4)
        if not (magic[:2] == b"MZ" or magic == b"\x7fELF") or exe.stat().st_size < 32 * 1024:
            raise RuntimeError("that file doesn't look like a program")
        return "ok"


class PkgToAnyPs5Manager(ToolManager):
    """pkg-to-anyps5 (extracts PS5 debug packages into the layout the AnyPS5 relinker expects) from the newest
    release of its GitHub project (Windows x86-64 only). Its own advice is to build it yourself, so this looks, in
    order, for: a copy in ./tools/pkg-to-anyps5, one installed by cargo (~/.cargo/bin) and one on PATH, and only then
    downloads the release. The download is checked against GitHub's digest for the file, or failing that against
    the release's SHA256SUMS.txt, and the binary is test-run (--version) before it is used."""

    KEY, NAME, SOURCE = "anyps5", "pkg-to-anyps5", "awake-devel/pkg-to-anyps5"
    SUBDIR, LEGACY_FOLDER, TAG_RE, EXE_NAME = "pkg-to-anyps5", "", r"v?\d+(?:\.\d+)+", "pkg-to-anyps5.exe"
    API_URL = "https://api.github.com/repos/awake-devel/pkg-to-anyps5/releases/latest"

    def _local_copy(self) -> Path | None:
        names = ("pkg-to-anyps5.exe", "pkg-to-anyps5")

        def unmanaged(d: Path) -> bool:
            return d.is_dir() and not d.name.startswith("dl_") and not re.fullmatch(self.TAG_RE, d.name)

        try:
            folders = [self.root, *sorted(d for d in self.root.iterdir() if unmanaged(d))]
        except OSError:
            folders = [self.root]
        try:
            folders.append(Path(os.environ.get("CARGO_HOME") or Path.home() / ".cargo") / "bin")
        except RuntimeError:  # no home folder to look in
            pass
        for folder in folders:
            for name in names:
                if (folder / name).is_file():
                    return folder / name
        found = shutil.which("pkg-to-anyps5")
        return Path(found) if found else None

    def _published_sha256(self, data: dict, asset: dict) -> str | None:
        """The asset's SHA-256: GitHub's own digest if it gives one, else the release's SHA256SUMS.txt."""
        digest = asset.get("digest") or ""
        if digest.startswith("sha256:"):
            return digest.split(":", 1)[1]
        sums = next((a for a in data.get("assets", []) if a["name"].lower() == "sha256sums.txt"), None)
        if not sums:
            return None
        try:
            req = urllib.request.Request(sums["browser_download_url"], headers={"User-Agent": self.USER_AGENT})
            with urllib.request.urlopen(req, timeout=10) as resp:
                text = resp.read(1 << 20).decode("utf-8", "replace")
        except Exception:
            return None
        for line in text.splitlines():  # "<hash>  <file name>" (a leading * marks binary mode)
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[1].strip().lstrip("*") == asset["name"] and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
                return parts[0]
        return None

    def _latest_release(self) -> Release:
        headers = {"User-Agent": self.USER_AGENT, "Accept": "application/vnd.github+json"}
        with urllib.request.urlopen(urllib.request.Request(self.API_URL, headers=headers), timeout=10) as resp:
            data = json.load(resp)
        tag = data["tag_name"]
        if not re.fullmatch(self.TAG_RE, tag):
            raise RuntimeError(f"unexpected release tag {tag!r}")
        windows = [a for a in data.get("assets", [])
                   if a["name"].lower().endswith(".zip") and "windows" in a["name"].lower() and "x86_64" in a["name"].lower()]
        if not windows:
            raise RuntimeError("no Windows package found in the latest pkg-to-anyps5 release")
        asset = windows[0]
        return Release(tag, asset["name"], asset["browser_download_url"], asset.get("size", 0),
                       self._published_sha256(data, asset))

    def _unpack(self, archive: Path, tmp: Path, dest: Path) -> None:
        staging = tmp / "extracted"
        staging.mkdir()
        self._extract_all(archive, staging)
        exes = [p for p in staging.rglob("*") if p.is_file() and p.suffix.lower() == ".exe"]
        found = (next((p for p in exes if p.name.lower() == self.EXE_NAME), None)
                 or next((p for p in exes if p.name.lower().startswith("pkg-to-anyps5")), None))
        if not found:
            raise RuntimeError("pkg-to-anyps5.exe is not in this package")
        if found.name != self.EXE_NAME:
            target = found.with_name(self.EXE_NAME)
            found.rename(target)
            found = target
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(found.parent, dest)

    @staticmethod
    def _probe(exe: Path) -> str:
        with exe.open("rb") as f:
            magic = f.read(4)
        if not (magic[:2] == b"MZ" or magic == b"\x7fELF"):
            raise RuntimeError("that file doesn't look like a program")
        try:  # a plain command-line tool, so asking its version is safe
            res = subprocess.run([str(exe), "--version"], capture_output=True, timeout=15, stdin=subprocess.DEVNULL,
                                 creationflags=SUBPROCESS_FLAGS)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"the program didn't start: {exc}") from exc
        text = (res.stdout + res.stderr).decode(errors="replace")
        if res.returncode != 0 and "pkg-to-anyps5" not in text.lower():
            raise RuntimeError("the downloaded pkg-to-anyps5 did not run")
        return text


class ZArchiveManager(ToolManager):
    """ZArchive (zarchive.exe, packs a folder into a .zar and unpacks one) from the newest release of Exzap's
    GitHub project (Windows only). A copy in ./tools/zarchive (or on PATH) is used as is. The release's file
    names are not assumed: a package or program for Windows is taken, or the one program there is."""

    KEY, NAME, SOURCE = "zarchive", "ZArchive", "Exzap/ZArchive"
    SUBDIR, LEGACY_FOLDER, TAG_RE, EXE_NAME = "zarchive", "", r"v?\d+(?:\.\d+)+", "zarchive.exe"
    API_URL = "https://api.github.com/repos/Exzap/ZArchive/releases/latest"

    def _local_copy(self) -> Path | None:
        def unmanaged(d: Path) -> bool:
            return d.is_dir() and not d.name.startswith("dl_") and not re.fullmatch(self.TAG_RE, d.name)

        try:
            folders = [self.root, *sorted(d for d in self.root.iterdir() if unmanaged(d))]
        except OSError:
            folders = [self.root]
        for folder in folders:
            for name in ("zarchive.exe", "zarchive"):
                if (folder / name).is_file():
                    return folder / name
        found = shutil.which("zarchive")
        return Path(found) if found else None

    def _latest_release(self) -> Release:
        headers = {"User-Agent": self.USER_AGENT, "Accept": "application/vnd.github+json"}
        with urllib.request.urlopen(urllib.request.Request(self.API_URL, headers=headers), timeout=10) as resp:
            data = json.load(resp)
        tag = data["tag_name"]
        if not re.fullmatch(self.TAG_RE, tag):
            raise RuntimeError(f"unexpected release tag {tag!r}")
        usable = [a for a in data.get("assets", []) if a["name"].lower().endswith((".zip", ".7z", ".exe"))]
        windows = ([a for a in usable if re.search(r"win", a["name"].lower())]
                   or [a for a in usable if a["name"].lower().endswith(".exe")])
        if not windows:
            raise ReleaseUnavailable("No Windows download found in the latest ZArchive release. Build it from "
                                     "github.com/Exzap/ZArchive, or select zarchive.exe yourself.")
        asset = windows[0]
        digest = asset.get("digest") or ""
        sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else None
        return Release(tag, asset["name"], asset["browser_download_url"], asset.get("size", 0), sha)

    def _unpack(self, archive: Path, tmp: Path, dest: Path) -> None:
        if archive.suffix.lower() == ".exe":  # the release is the program itself
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(archive, dest / self.EXE_NAME)
            return
        staging = tmp / "extracted"
        staging.mkdir()
        self._extract_all(archive, staging)
        exes = [p for p in staging.rglob("*") if p.is_file() and p.suffix.lower() == ".exe"]
        found = next((p for p in exes if p.name.lower() == self.EXE_NAME), None)
        if not found:
            raise RuntimeError("zarchive.exe is not in this ZArchive package")
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(found.parent, dest)  # keep the whole folder: the program may need DLLs beside it

    @staticmethod
    def _probe(exe: Path) -> str:
        """With no arguments zarchive prints its usage text and exits; it never starts packing."""
        with exe.open("rb") as f:
            magic = f.read(4)
        if not (magic[:2] == b"MZ" or magic == b"\x7fELF"):
            raise RuntimeError("that file doesn't look like a program")
        try:
            res = subprocess.run([str(exe)], capture_output=True, timeout=15, stdin=subprocess.DEVNULL,
                                 creationflags=SUBPROCESS_FLAGS)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"the program didn't start: {exc}") from exc
        text = (res.stdout + res.stderr).decode(errors="replace")
        if "zarchive" not in text.lower():
            raise RuntimeError("the downloaded zarchive did not run")
        return text


class Ps3QddManager(ToolManager):
    """PS3Dec (PS3Dec.exe, the PS3 disc decryptor inside ElektroStudios' PS3 Quick Disc Decryptor) from the newest
    release of that GitHub project (Windows only). The package is kept whole, so its Keys folder of Redump .dkey
    files is there for the PS3 tab. A PS3Dec.exe you put in ./tools/ps3qdd (or an unpacked PS3QDD folder there) is
    used as is. The release's file names are not assumed: the package with keys is taken if there is one."""

    KEY, NAME, SOURCE = "ps3qdd", "PS3QDD", "ElektroStudios/PS3-Quick-Disc-Decryptor"
    SUBDIR, LEGACY_FOLDER, TAG_RE, EXE_NAME = "ps3qdd", "", r"v?\d+(?:\.\d+)+", "PS3Dec.exe"
    API_URL = "https://api.github.com/repos/ElektroStudios/PS3-Quick-Disc-Decryptor/releases/latest"

    def _local_copy(self) -> Path | None:
        def unmanaged(d: Path) -> bool:
            return d.is_dir() and not d.name.startswith("dl_") and not re.fullmatch(self.TAG_RE, d.name)

        try:
            folders = [self.root, *sorted(d for d in self.root.iterdir() if unmanaged(d))]
        except OSError:
            folders = [self.root]
        for folder in folders:
            for name in ("PS3Dec.exe", "ps3dec.exe", "PS3Dec", "ps3dec"):
                if (folder / name).is_file():
                    return folder / name
        found = shutil.which("ps3dec")
        return Path(found) if found else None

    def _latest_release(self) -> Release:
        headers = {"User-Agent": self.USER_AGENT, "Accept": "application/vnd.github+json"}
        with urllib.request.urlopen(urllib.request.Request(self.API_URL, headers=headers), timeout=10) as resp:
            data = json.load(resp)
        tag = data["tag_name"]
        if not re.fullmatch(self.TAG_RE, tag):
            raise RuntimeError(f"unexpected release tag {tag!r}")
        usable = [a for a in data.get("assets", []) if a["name"].lower().endswith((".zip", ".7z"))]
        if not usable:
            raise ReleaseUnavailable("No download found in the latest PS3QDD release. Get it from "
                                     "github.com/ElektroStudios/PS3-Quick-Disc-Decryptor, or select PS3Dec.exe yourself.")
        asset = next((a for a in usable if "key" in a["name"].lower()), usable[0])  # the package with the keys
        digest = asset.get("digest") or ""
        sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else None
        return Release(tag, asset["name"], asset["browser_download_url"], asset.get("size", 0), sha)

    def _unpack(self, archive: Path, tmp: Path, dest: Path) -> None:
        staging = tmp / "extracted"
        staging.mkdir()
        self._extract_all(archive, staging)
        found = next((p for p in staging.rglob("*") if p.is_file() and p.name.lower() == self.EXE_NAME.lower()), None)
        if not found:
            raise RuntimeError("PS3Dec.exe is not in this PS3QDD package")
        root = staging
        while (kids := list(root.iterdir())) and len(kids) == 1 and kids[0].is_dir():  # a single wrapper folder
            root = kids[0]
        rel = found.relative_to(root)
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(root, dest)  # keep everything: the Keys folder sits beside the program
        if not (dest / self.EXE_NAME).is_file():
            shutil.copy2(dest / rel, dest / self.EXE_NAME)

    @staticmethod
    def _probe(exe: Path) -> str:
        """PS3Dec waits for a key press, so it is not started here: the file just has to be a program."""
        with exe.open("rb") as f:
            magic = f.read(4)
        if not (magic[:2] == b"MZ" or magic == b"\x7fELF"):
            raise RuntimeError("that file doesn't look like a program")
        return ""


class XdvdfsManager(ToolManager):
    """xdvdfs (xdvdfs.exe, packs and checksums Xbox ISOs) from the newest release of antangelo's GitHub project
    (Windows only). A copy in ./tools/xdvdfs (or on PATH) is used as is. The release's file names are not assumed:
    the Windows package is the one with "windows" in its name, else the one program there is."""

    KEY, NAME, SOURCE = "xdvdfs", "xdvdfs", "antangelo/xdvdfs"
    SUBDIR, LEGACY_FOLDER, TAG_RE, EXE_NAME = "xdvdfs", "", r"v?\d+(?:\.\d+)+", "xdvdfs.exe"
    API_URL = "https://api.github.com/repos/antangelo/xdvdfs/releases/latest"

    def _local_copy(self) -> Path | None:
        def unmanaged(d: Path) -> bool:
            return d.is_dir() and not d.name.startswith("dl_") and not re.fullmatch(self.TAG_RE, d.name)

        try:
            folders = [self.root, *sorted(d for d in self.root.iterdir() if unmanaged(d))]
        except OSError:
            folders = [self.root]
        for folder in folders:
            for name in ("xdvdfs.exe", "xdvdfs"):
                if (folder / name).is_file():
                    return folder / name
        found = shutil.which("xdvdfs")
        return Path(found) if found else None

    def _latest_release(self) -> Release:
        headers = {"User-Agent": self.USER_AGENT, "Accept": "application/vnd.github+json"}
        with urllib.request.urlopen(urllib.request.Request(self.API_URL, headers=headers), timeout=10) as resp:
            data = json.load(resp)
        tag = data["tag_name"]
        if not re.fullmatch(self.TAG_RE, tag):
            raise RuntimeError(f"unexpected release tag {tag!r}")
        usable = [a for a in data.get("assets", []) if a["name"].lower().endswith((".zip", ".7z", ".exe"))]
        windows = [a for a in usable if "win" in a["name"].lower()] or [a for a in usable if a["name"].lower().endswith(".exe")]
        if not windows:
            raise ReleaseUnavailable("No Windows download found in the latest xdvdfs release. Get it from "
                                     "github.com/antangelo/xdvdfs, or select xdvdfs.exe yourself.")
        asset = windows[0]
        digest = asset.get("digest") or ""
        sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else None
        return Release(tag, asset["name"], asset["browser_download_url"], asset.get("size", 0), sha)

    def _unpack(self, archive: Path, tmp: Path, dest: Path) -> None:
        if archive.suffix.lower() == ".exe":  # the release is the program itself
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(archive, dest / self.EXE_NAME)
            return
        staging = tmp / "extracted"
        staging.mkdir()
        self._extract_all(archive, staging)
        exes = [p for p in staging.rglob("*") if p.is_file() and p.suffix.lower() == ".exe"]
        found = next((p for p in exes if p.name.lower() == self.EXE_NAME), None) or (exes[0] if len(exes) == 1 else None)
        if not found:
            raise RuntimeError("xdvdfs.exe is not in this xdvdfs package")
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True)
        shutil.copy2(found, dest / self.EXE_NAME)

    @staticmethod
    def _probe(exe: Path) -> str:
        """`xdvdfs -h` only prints its usage text."""
        with exe.open("rb") as f:
            magic = f.read(4)
        if not (magic[:2] == b"MZ" or magic == b"\x7fELF"):
            raise RuntimeError("that file doesn't look like a program")
        try:
            res = subprocess.run([str(exe), "-h"], capture_output=True, timeout=15, stdin=subprocess.DEVNULL,
                                 creationflags=SUBPROCESS_FLAGS)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"the program didn't start: {exc}") from exc
        text = (res.stdout + res.stderr).decode(errors="replace")
        if "xdvdfs" not in text.lower() and "xiso" not in text.lower():
            raise RuntimeError("the downloaded xdvdfs did not run")
        return text


class AnyPS5Manager(ToolManager):
    """AnyPS5's relinker (relinker.exe) and its system libraries (libs/*.prx) from the newest GitHub release that
    has both for Windows. They are taken from the SAME release, as AnyPS5 requires, into ./tools/anyps5/<tag>/.
    A relinker.exe you put in ./tools/anyps5 yourself (e.g. built from source) is used as is; its libraries are found
    beside it or in the source build's build/core/libs/libs."""

    KEY, NAME, SOURCE = "relinker", "AnyPS5 relinker", "boykopovar/AnyPS5"
    SUBDIR, LEGACY_FOLDER, TAG_RE, EXE_NAME = "anyps5", "", r"v?\d+(?:\.\d+)+", "relinker.exe"
    API_URL = "https://api.github.com/repos/boykopovar/AnyPS5/releases?per_page=10"

    def __init__(self, emit: Callable[[tuple], None]):
        super().__init__(emit)
        self._libs_release: Release | None = None  # the libraries' download, found alongside the relinker's

    def _local_copy(self) -> Path | None:
        def unmanaged(d: Path) -> bool:
            return d.is_dir() and not d.name.startswith("dl_") and not re.fullmatch(self.TAG_RE, d.name)

        try:
            folders = [self.root, *sorted(d for d in self.root.iterdir() if unmanaged(d))]
        except OSError:
            folders = [self.root]
        for folder in folders:
            try:
                found = next(iter(sorted(folder.glob("relinker*.exe"))), None)
            except OSError:
                found = None
            if found:
                return found
        return None

    def _latest_release(self) -> Release:
        headers = {"User-Agent": self.USER_AGENT, "Accept": "application/vnd.github+json"}
        with urllib.request.urlopen(urllib.request.Request(self.API_URL, headers=headers), timeout=10) as resp:
            releases = json.load(resp)

        def asset(a: dict, tag: str) -> Release:
            digest = a.get("digest") or ""
            sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else None
            return Release(tag, a["name"], a["browser_download_url"], a.get("size", 0), sha)

        for rel in releases:
            tag = rel.get("tag_name", "")
            if rel.get("draft") or not re.fullmatch(self.TAG_RE, tag):
                continue
            assets = rel.get("assets", [])
            exe = next((a for a in assets if re.fullmatch(r"relinker.*\.exe", a["name"], re.I)), None)
            libs = next((a for a in assets if re.fullmatch(r"prx-windows.*\.zip", a["name"], re.I)), None)
            if exe and libs:
                self._libs_release = asset(libs, tag)
                return asset(exe, tag)
        raise ReleaseUnavailable("No AnyPS5 release with a Windows relinker and system libraries was found. Build "
                                 "AnyPS5 from github.com/boykopovar/AnyPS5, or select relinker.exe yourself.")

    def _unpack(self, archive: Path, tmp: Path, dest: Path) -> None:
        libs_release = self._libs_release
        if libs_release is None:
            raise RuntimeError("the libraries' download details were lost")
        shutil.rmtree(dest, ignore_errors=True)
        try:
            dest.mkdir(parents=True)
            shutil.copy2(archive, dest / self.EXE_NAME)  # relinker-vX.Y.Z.exe -> relinker.exe
            zip_path = tmp / libs_release.name
            self._download(libs_release, zip_path)
            staging = tmp / "libs_extracted"
            staging.mkdir()
            self._extract_all(zip_path, staging)
            holders = [staging, *sorted(d for d in staging.rglob("*") if d.is_dir())]
            libs = next((d for d in holders if any(d.glob("*.prx"))), None)  # the .prx files (and the DLLs beside them)
            if libs is None:
                raise RuntimeError("no .prx libraries are in the AnyPS5 package")
            shutil.copytree(libs, dest / "libs")
        except Exception:
            shutil.rmtree(dest, ignore_errors=True)
            raise

    @staticmethod
    def _probe(exe: Path) -> str:
        """--help prints the usage text and exits 0; nothing is relinked."""
        with exe.open("rb") as f:
            magic = f.read(4)
        if not (magic[:2] == b"MZ" or magic == b"\x7fELF"):
            raise RuntimeError("that file doesn't look like a program")
        try:
            res = subprocess.run([str(exe), "--help"], capture_output=True, timeout=15, stdin=subprocess.DEVNULL,
                                 creationflags=SUBPROCESS_FLAGS)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"the program didn't start: {exc}") from exc
        text = (res.stdout + res.stderr).decode(errors="replace")
        if "relinker" not in text.lower() and "usage" not in text.lower():
            raise RuntimeError("the downloaded relinker did not run")
        return text
