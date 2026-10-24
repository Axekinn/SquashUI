"""Organisation tools: multi-disc grouping, leftovers, duplicates, playlist repair and name tidying,
with preview, apply and undo."""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

from .constants import APP_DIR, ILLEGAL_NAME_CHARS
from .fingerprint import find_same_content
from .formats import associated_files
from .models import fmt_size
from .system import file_size


ORGANIZE_EXTENSIONS = frozenset({".chd", ".cue", ".gdi", ".iso", ".cso", ".zso", ".rvz", ".gcm", ".wbfs", ".gcz",
                                 ".wia", ".cdi", ".toc", ".nrg", ".pbp"})


SIDECAR_EXTENSIONS = frozenset({".sbi", ".lsd", ".sub", ".ppf"})  # small per-disc files that travel with the disc


# "(Disc 1)", "(Disc 2 of 3)", "[CD2]", "(Disk 1)" - or the same unbracketed ("Game Disc 1").
DISC_RE = re.compile(
    r"[(\[]\s*(?:disc|disk|cd)\s*[-_ ]?(\d{1,2})(?:\s*(?:of|/)\s*(\d{1,2}))?\s*[)\]]"
    r"|(?<![A-Za-z0-9])(?:disc|disk|cd)\s*[-_ ]?(\d{1,2})(?:\s*of\s*(\d{1,2}))?(?![A-Za-z0-9])",
    re.IGNORECASE,
)


# "(Disc 1 - Leon)" -> "(Disc 1) (Leon)": a label inside the marker is treated like one after it.
MARKER_LABEL_RE = re.compile(r"([(\[]\s*(?:disc|disk|cd)\s*[-_ ]?\d{1,2}(?:\s*(?:of|/)\s*\d{1,2})?)\s*[-,:]\s*"
                             r"([^()\[\]]+?)\s*([)\]])", re.IGNORECASE)


PAREN_TAG_RE = re.compile(r"\s*\(([^()]*)\)")


_REGION_WORDS = (r"usa|europe|japan|world|asia|korea|china|taiwan|hong kong|brazil|australia|canada|france|germany|spain|"
                 r"italy|uk|netherlands|sweden|denmark|norway|finland|russia|mexico|argentina|scandinavia|latin america|"
                 r"portugal|poland|greece|new zealand|india|israel|south africa|turkey|ntsc|pal|ntsc-u|ntsc-j|pal-e")


_LANGUAGE_CODES = (r"en|fr|de|es|it|nl|pt|sv|no|da|fi|ja|ko|zh|ru|pl|ca|cs|hu|tr|el|ar|he|en-gb|en-us|pt-br|"
                   r"zh-hans|zh-hant")


STANDARD_TAG_RE = re.compile(rf"^(?:{_REGION_WORDS}|{_LANGUAGE_CODES}|rev\s*[0-9a-z.]+|v\s*\d[\d.]*[a-z]?|beta\s*\d*|"
                             r"proto(?:type)?\s*\d*|demo|sample|unl|alt\s*\d*|pirate|bios|track\s*\d+)$", re.IGNORECASE)


def is_standard_tag(content: str) -> bool:
    """True for the kind of tag that says WHICH release a file is (region, language, revision, beta...): \"USA\",
    \"En,Fr,De\", \"Rev 1\". Anything else in brackets is free text: a disc's label (\"Leon\"), an edition name..."""
    parts = [p for p in re.split(r"\s*[,+/]\s*", content.strip()) if p]
    return bool(parts) and all(STANDARD_TAG_RE.match(p) for p in parts)


def parse_disc(stem: str) -> tuple[str, int, int | None] | None:
    """('Game (USA)', 2, 3) for 'Game (USA) (Disc 2 of 3)'; None when the name has no disc marker.

    Region, revision and language tags are kept, so a USA disc 1 is never paired with a Europe disc 2. A free-text
    label straight after the marker is the disc's own title, not the game's, and is dropped: 'Resident Evil 2 (Disc 1)
    (Leon)' and '... (Disc 2)(Claire)' are both 'Resident Evil 2'.
    """
    stem = MARKER_LABEL_RE.sub(r"\1\3 (\2)", stem)
    matches = list(DISC_RE.finditer(stem))
    if not matches:
        return None
    m = matches[-1]
    number = int(m[1] or m[3])
    total = m[2] or m[4]
    rest = stem[m.end():]
    while (label := PAREN_TAG_RE.match(rest)) and not is_standard_tag(label[1]):
        rest = rest[label.end():]
    base = re.sub(r"\s+", " ", f"{stem[:m.start()]} {rest}").strip(" -_.,")
    return (base, number, int(total) if total else None) if base and number >= 1 else None


ANY_TAG_RE = re.compile(r"\s*[(\[][^)\]]*[)\]]")


ROMAN_RE = re.compile(r"^(?:i{1,3}|iv|vi{0,3}|ix|xi{0,3}|xiv|xv|xvi{0,3}|xix|xx)$")


def title_core(stem: str) -> tuple[str, int | None]:
    """(the game's title boiled down for comparing, its disc number or None): no tags, punctuation, case or leading
    'The', so 'Legend of Zelda, The (USA) [!]' and 'The Legend of Zelda' compare equal."""
    parsed = parse_disc(stem)
    base, number = (parsed[0], parsed[1]) if parsed else (stem, None)
    text = ANY_TAG_RE.sub("", base)
    if m := ARTICLE_RE.match(text):
        text = f"{m[2]} {m[1]}{m[3]}"
    words = re.sub(r"[^\w\s]", " ", text.replace("_", " ").replace("&", " and ").casefold()).split()
    if words and words[0] == "the":
        words = words[1:]
    return " ".join(words), number


def release_tags(name: str) -> tuple[str, ...]:
    """The region / language / revision tags of a name, normalised: two files only belong together if these agree."""
    return tuple(sorted(t.casefold().strip() for t in re.findall(r"[(\[]([^)\]]*)[)\]]", name) if is_standard_tag(t)))


def similar_titles(a: str, b: str) -> bool:
    """Same game, spelled a little differently? Similarity alone can't tell 'Final Fantasy VII' from 'VIII' (0.97),
    so the numbers in the two titles must match exactly: a sequel is never a spelling variant."""
    if a == b:
        return True
    numerals = lambda t: [w for w in t.split() if w.isdigit() or ROMAN_RE.match(w)]  # noqa: E731
    if numerals(a) != numerals(b):
        return False
    if SequenceMatcher(None, a, b).ratio() >= 0.8:
        return True
    ta, tb = a.split(), b.split()
    shared = 0
    while shared < min(len(ta), len(tb)) and ta[shared] == tb[shared]:
        shared += 1
    if shared == min(len(ta), len(tb)):  # one is the other plus a word or two ("... Dual Shock")
        return shared > 0 and max(len(ta), len(tb)) - shared <= 2
    # The same multi-word title followed by different short labels ("Big Story Hero" / "Big Story Villain").
    return shared >= 2 and shared * 2 >= max(len(ta), len(tb)) and len(ta) - shared <= 2 and len(tb) - shared <= 2


def _common_title(bases: list[str]) -> str:
    """The shared start of several spellings of one title, plus the release tags (region...) they agree on."""
    prefix: list[str] = []
    for words in zip(*(b.split() for b in bases)):
        if len({w.casefold() for w in words}) != 1:
            break
        prefix.append(words[0])
    name = " ".join(prefix).strip(" -_.,")
    name = ANY_TAG_RE.sub("", name).strip(" -_.,") if "(" in name or "[" in name else name
    if len(name) < 3:
        return min(bases, key=len)
    tags = [t for t in re.findall(r"[(\[][^)\]]*[)\]]", bases[0]) if is_standard_tag(t[1:-1])]
    return " ".join([name, *tags])


def folder_name_for(base: str) -> str:
    return ILLEGAL_NAME_CHARS.sub("", base).strip(" .")


@dataclass(eq=False, slots=True)
class DiscSet:
    parent: Path  # folder the discs are in now
    name: str  # folder (and playlist) name
    discs: dict[int, Path]  # disc number -> its main file (.chd / .cue / .gdi / .iso ...)
    target: Path  # folder the set ends up in
    moves: list[tuple[Path, Path]] = field(default_factory=list)  # (from, to) for every file that moves
    playlist: Path | None = None  # None: the playlist already exists
    problem: str = ""  # why this set can't be organized (the row is shown but can't be ticked)
    candidates: dict[int, list[Path]] = field(default_factory=dict)  # every file found for each disc number
    expected: int = 0  # how many discs the game has (highest number seen, or the "of N" in the names)
    extras: dict[int, int] = field(default_factory=dict)  # disc number -> track / sidecar files that travel with it
    loose: bool = False  # paired by similar names rather than identical ones: shown, but not ticked

    @property
    def note(self) -> str:
        if self.problem:
            return self.problem
        text = "Ready" if self.moves else "Already in its folder - playlist only"
        return text + "  |  similar names: check the discs" if self.loose else text


def _disc_files(main: Path) -> list[Path]:
    """The disc's main file plus the track files and sidecars that must stay with it."""
    files = sorted(associated_files(main) - {main})
    files += [p for p in sorted(main.parent.iterdir())
              if p.is_file() and p != main and p.stem == main.stem and p.suffix.lower() in SIDECAR_EXTENSIONS]
    return [main, *dict.fromkeys(files)]


def _is_incomplete(entries: list[tuple[int, int | None, Path, str]]) -> bool:
    numbers = {e[0] for e in entries}
    expected = max(max(numbers), max((e[1] or 0 for e in entries), default=0))
    return expected < 2 or numbers != set(range(1, expected + 1))


def _merge_loose(groups: dict[tuple[Path, str], list]) -> tuple[dict[tuple[Path, str], list], set[tuple[Path, str]]]:
    """Join, within a folder, groups that can't be complete sets on their own but look like parts of one game: no disc
    number in common, the same region / revision tags, and titles that are similar (see similar_titles)."""
    by_folder: dict[Path, list[tuple[Path, str]]] = {}
    for key, entries in groups.items():
        if _is_incomplete(entries):
            by_folder.setdefault(key[0], []).append(key)
    merged, loose = dict(groups), set()
    for folder, keys in by_folder.items():
        parent = {k: k for k in keys}

        def root_of(k):
            while parent[k] != k:
                k = parent[k]
            return k

        info = {k: (title_core(groups[k][0][3])[0], release_tags(groups[k][0][3]), {e[0] for e in groups[k]}) for k in keys}
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                (ta, tags_a, nums_a), (tb, tags_b, nums_b) = info[a], info[b]
                if tags_a == tags_b and not nums_a & nums_b and similar_titles(ta, tb):
                    parent[root_of(b)] = root_of(a)
        clusters: dict[tuple[Path, str], list] = {}
        for k in keys:
            clusters.setdefault(root_of(k), []).append(k)
        for members in clusters.values():
            if len(members) < 2:
                continue
            entries = [e for k in members for e in groups[k]]
            if len({e[0] for e in entries}) != len(entries):
                continue  # two files for one disc number: leave the strict result alone
            name = _common_title([e[3] for e in entries])
            for k in members:
                del merged[k]
            new_key = (folder, name.casefold())
            if new_key in merged:
                continue
            merged[new_key] = [(n, t, p, name) for n, t, p, _b in entries]
            loose.add(new_key)
    return merged, loose


def scan_multidisc(root: Path, recursive: bool, stop: threading.Event, loose: bool = False) -> list[DiscSet]:
    """Find multi-disc games under root. Nothing is moved; the result is the plan. With `loose`, discs whose names are
    only similar are paired too (flagged on the set so the caller can leave them unticked)."""
    groups: dict[tuple[Path, str], list[tuple[int, int | None, Path, str]]] = {}
    for folder, _subdirs, filenames in _walk(root, recursive, stop):
        mains, _owned = _disc_mains(folder, filenames)
        for path in mains:  # track files belonging to a .cue / .gdi are not discs of their own
            if (parsed := parse_disc(path.stem)) is not None:
                base, number, total = parsed
                groups.setdefault((folder, base.casefold()), []).append((number, total, path, base))

    loose_keys: set[tuple[Path, str]] = set()
    if loose:
        groups, loose_keys = _merge_loose(groups)
    sets, claimed = [], set()
    for (folder, _key), entries in sorted(groups.items(), key=lambda kv: (str(kv[0][0]).lower(), kv[0][1])):
        numbers = sorted({e[0] for e in entries})
        declared = max((e[1] or 0 for e in entries), default=0)
        if len(numbers) < 2 and declared < 2:
            continue  # a lone "Disc 1" is just a single-disc game
        name = folder_name_for(entries[0][3])
        if not name:
            continue
        by_number: dict[int, list[Path]] = {}
        for number, _t, path, _b in entries:
            by_number.setdefault(number, []).append(path)
        target = folder if folder.name.casefold() == name.casefold() else folder / name
        expected = max(numbers[-1], declared)
        dset = DiscSet(folder, name, {n: ps[0] for n, ps in by_number.items()}, target,
                       candidates=by_number, expected=expected, loose=(folder, _key) in loose_keys)

        missing = [n for n in range(1, expected + 1) if n not in by_number]
        dupes = [n for n, ps in by_number.items() if len(ps) > 1]
        key = (folder, name.casefold())
        if missing:
            dset.problem = "Missing disc " + ", ".join(map(str, missing)) + f" of {expected} - left alone"
        elif dupes:
            dset.problem = f"Disc {dupes[0]} has more than one file ({', '.join(p.name for p in by_number[dupes[0]])})"
        elif key in claimed:
            dset.problem = "Another set in this folder maps to the same folder name"
        claimed.add(key)

        if not dset.problem:
            if target.exists() and not target.is_dir():
                dset.problem = f"A file named '{name}' is in the way"
            for number in sorted(dset.discs):
                files = _disc_files(dset.discs[number])
                dset.extras[number] = len(files) - 1
                for f in files:
                    dest = target / f.relative_to(folder)
                    if dest == f:
                        continue
                    if dest.exists():
                        dset.problem = dset.problem or f"'{dest.name}' already exists in the target folder"
                    dset.moves.append((f, dest))
            playlist = target / f"{name}.m3u"
            if not playlist.exists():
                dset.playlist = playlist
            elif not dset.moves:
                continue  # already organized: folder, discs and playlist are all there
        sets.append(dset)
    return sets


# ---- Generic findings: what every Organize tool previews and applies ---------------------------------------
HOLDING_DIR = "_SquashUI_removed"  # removed files wait here (under the scanned folder) instead of being deleted


LEGACY_HOLDING_DIR = "_qbSquash_removed"  # folders left by v10.0 and earlier: still skipped when scanning


COMPRESSED_EXTENSIONS = frozenset({".chd", ".rvz", ".cso", ".zso", ".gcz", ".wia"})


SOURCE_EXTENSIONS = frozenset({".cue", ".gdi", ".iso", ".nrg", ".cdi", ".toc", ".gcm"})


SINGLE_FILE_EXTENSIONS = frozenset({".chd", ".iso", ".cso", ".zso", ".rvz", ".gcm", ".wbfs", ".gcz", ".wia",
                                    ".cdi", ".nrg", ".pbp"})  # disc images that are one file (safe to rename)


# First bytes of each compressed format: proof a "converted copy" is real before its source is offered for removal.
IMAGE_MAGIC = {".chd": b"MComprHD", ".rvz": b"RVZ\x01", ".cso": b"CISO", ".zso": b"ZISO",
               ".gcz": b"\x01\xc0\x0b\xb1", ".wia": b"WIA\x01"}


TRACK_RE = re.compile(r"\(Track \d+\)", re.IGNORECASE)


ARTICLE_RE = re.compile(r"^(.*?),\s+(The|A|An)(?=\s*[(\[-]|$)(.*)$")


BRACKET_TAG_RE = re.compile(r"\s*\[([^\]]*)\]")


DISC_TAG_RE = re.compile(r"\s*\[\s*(?:disc|disk|cd)\s*[-_ ]?\d", re.IGNORECASE)


DUPE_POLICIES = ("Prefer compressed (CHD / RVZ / CSO), then newest", "Keep the newest", "Keep the largest",
                 "Keep the smallest")


@dataclass(eq=False, slots=True)
class Finding:
    """One row of an Organize preview: what would change, and in ops exactly how."""
    title: str
    count: str = ""
    note: str = "Ready"
    children: list[tuple[str, str, str]] = field(default_factory=list)  # (name, count column, note) per detail row
    ops: list[tuple] = field(default_factory=list)  # ("mkdir"|"move"|"write"|"replace_line"|"rmdir"|"delete", ...)
    problem: str = ""  # why it can't be applied (the row is shown but can't be ticked)
    checked: bool = True  # ticked by default
    size: int = 0  # bytes it frees, where that applies

    @property
    def status(self) -> str:
        return self.problem or self.note


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _m3u_key(line: str) -> str:
    return line.strip().replace("\\", "/").removeprefix("./").casefold()


def _read_text(path: Path) -> str:
    # surrogateescape keeps playlists in a legacy code page byte-for-byte intact when they are written back
    return path.read_text(encoding="utf-8-sig", errors="surrogateescape")


def _write_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(text, encoding="utf-8", errors="surrogateescape", newline="\n")
    os.replace(tmp, path)


def _walk(root: Path, recursive: bool, stop: threading.Event):
    """(folder, subfolder names, file names) for root and, if recursive, everything below it. The holding
    folder of removed files is never entered."""
    for dirpath, dirnames, filenames in os.walk(root):
        if stop.is_set():
            return
        subdirs = list(dirnames)
        if recursive:
            dirnames[:] = [d for d in dirnames if d not in (HOLDING_DIR, LEGACY_HOLDING_DIR)]
        else:
            dirnames.clear()
        yield Path(dirpath), subdirs, filenames


def _disc_mains(folder: Path, filenames: list[str]) -> tuple[list[Path], set[Path]]:
    """The disc images in a folder, leaving out track files that belong to a .cue / .gdi (and those track files)."""
    present = [folder / n for n in filenames if Path(n).suffix.lower() in ORGANIZE_EXTENSIONS]
    owned = {t for p in present if p.suffix.lower() in (".cue", ".gdi") for t in associated_files(p) - {p}}
    return [p for p in present if p not in owned], owned


def _removal_ops(files: list[Path], root: Path, permanent: bool) -> list[tuple]:
    """Move files into the holding folder (undoable), or delete them outright."""
    ops: list[tuple] = []
    for f in files:
        if permanent:
            ops.append(("delete", str(f)))
            continue
        dest = root / HOLDING_DIR / _rel(f, root)
        n = 2
        while dest.exists():
            dest = dest.with_name(f"{f.stem} ({n}){f.suffix}")
            n += 1
        ops.append(("move", str(f), str(dest)))
    return ops


def _run_op(op: tuple, done: list[dict]) -> None:
    """Carry out one operation, recording what is needed to reverse it in done."""
    kind = op[0]
    if kind == "mkdir":
        d, chain = Path(op[1]), []
        while not d.exists():
            chain.append(d)
            d = d.parent
        for d in reversed(chain):
            d.mkdir()
            done.append({"op": "mkdir", "path": str(d)})
    elif kind == "move":
        src, dst = Path(op[1]), Path(op[2])
        if dst.exists():
            raise FileExistsError(f"{dst.name} already exists")
        _run_op(("mkdir", str(dst.parent)), done)
        shutil.move(str(src), str(dst))
        done.append({"op": "move", "src": str(src), "dst": str(dst)})
    elif kind == "write":
        path = Path(op[1])
        old = _read_text(path) if path.exists() else None
        _write_text(path, op[2])
        done.append({"op": "write", "path": str(path), "old": old})
    elif kind == "replace_line":  # in a playlist: the line naming op[2] now names op[3]
        path = Path(op[1])
        if not path.exists():
            return
        old = _read_text(path)
        lines = old.split("\n")
        hits = [i for i, line in enumerate(lines) if _m3u_key(line) == _m3u_key(op[2])]
        if hits:
            for i in hits:
                lines[i] = op[3]
            _write_text(path, "\n".join(lines))
            done.append({"op": "write", "path": str(path), "old": old})
    elif kind == "rmdir":
        Path(op[1]).rmdir()
        done.append({"op": "rmdir", "path": op[1]})
    elif kind == "delete":
        Path(op[1]).unlink()  # permanent: nothing to record
    else:
        raise ValueError(f"unknown operation {kind!r}")


def _undo_ops(done: list[dict]) -> list[str]:
    """Reverse recorded operations, newest first. Returns what couldn't be put back."""
    problems: list[str] = []
    for e in reversed(done):
        try:
            match e["op"]:
                case "move":
                    src, dst = Path(e["src"]), Path(e["dst"])
                    if not dst.exists():
                        problems.append(f"{dst.name} is no longer there")
                    elif src.exists():
                        problems.append(f"{src.name}: something else is at the original location")
                    else:
                        src.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(dst), str(src))
                case "write":
                    path = Path(e["path"])
                    if e["old"] is None:
                        path.unlink(missing_ok=True)
                    else:
                        _write_text(path, e["old"])
                case "mkdir":
                    with contextlib.suppress(OSError):
                        Path(e["path"]).rmdir()  # only succeeds when empty
                case "rmdir":
                    Path(e["path"]).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            problems.append(f"{Path(e.get('path') or e.get('dst') or '?').name}: {exc}")
    return problems


def undo_file(tool: str) -> Path:
    return APP_DIR / f"squashui_undo_{tool}.json"


def _save_undo(tool: str, items: list[dict]) -> None:
    try:
        undo_file(tool).write_text(json.dumps({"made": datetime.now().isoformat(timespec="seconds"),
                                               "items": items}, indent=1), encoding="utf-8")
    except OSError:
        pass  # the undo record is a convenience; never fail the run because of it


def apply_findings(tool: str, findings: list[Finding], log: Callable[[str], None],
                   progress: Callable[[int, int], None], stop: threading.Event,
                   on_result: Callable[[int, str], None] | None = None) -> tuple[int, list[tuple[str, str]]]:
    """Apply each finding's operations. A finding that fails part-way is put back. Every change is recorded in the
    tool's undo file after each finding, so a run can be reversed."""
    items: list[dict] = []
    failures: list[tuple[str, str]] = []
    ok = 0
    for n, finding in enumerate(findings, 1):
        if stop.is_set():
            break
        done: list[dict] = []
        if on_result:
            on_result(n - 1, "Working...")
        try:
            for op in finding.ops:
                _run_op(op, done)
            ok += 1
            log(f"Done: {finding.title}")
            if on_result:
                on_result(n - 1, "Done")
        except Exception as exc:
            _undo_ops(done)
            done = []
            failures.append((finding.title, str(exc) or type(exc).__name__))
            log(f"FAILED: {finding.title} - {exc} (put back)")
            if on_result:
                on_result(n - 1, f"Failed: {exc or type(exc).__name__} (put back)")
        if done:
            items.append({"name": finding.title, "ops": done})
            _save_undo(tool, items)
        progress(n, len(findings))
    return ok, failures


def undo_last(tool: str) -> tuple[int, list[str]] | None:
    """(items restored, problems) for the tool's last run; None when there is nothing to undo."""
    try:
        data = json.loads(undo_file(tool).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    items = [i for i in data.get("items", []) if isinstance(i, dict)]
    problems: list[str] = []
    for item in reversed(items):
        problems += _undo_ops(item.get("ops", []))
    undo_file(tool).unlink(missing_ok=True)
    return len(items), problems


# ---- Tool 1: multi-disc games --------------------------------------------------------------------------------
def discset_finding(d: DiscSet) -> Finding:
    kids: list[tuple[str, str, str]] = []
    for number in range(1, d.expected + 1):
        files = d.candidates.get(number)
        if not files:
            kids.append(("(not found)", f"Disc {number}", "Missing"))
            continue
        for path in files:
            extra = d.extras.get(number, 0)
            kids.append((path.name, f"Disc {number}",
                         "Duplicate" if len(files) > 1 else f"+{extra} track / extra file(s)" if extra else ""))
    ops: list[tuple] = []
    if not d.problem:
        if not d.target.exists():
            ops.append(("mkdir", str(d.target)))
        ops += [("move", str(a), str(b)) for a, b in d.moves]
        if d.playlist is not None:
            ops.append(("write", str(d.playlist), "\n".join(d.discs[n].name for n in sorted(d.discs)) + "\n"))
    where = d.name if d.target != d.parent else f"{d.name}  (in place)"
    return Finding(where, str(len(d.discs)), d.note, kids, ops, d.problem, checked=not d.loose)


def find_multidisc(root: Path, recursive: bool, opts: dict, stop: threading.Event) -> tuple[list[Finding], str]:
    found = [discset_finding(d) for d in scan_multidisc(root, recursive, stop, bool(opts.get("loose")))]
    return found, ("similar-name matches are left unticked" if any(not f.checked for f in found) else "")


# ---- Tool 2: clean up leftovers ------------------------------------------------------------------------------
def _looks_converted(path: Path) -> bool:
    """A compressed image that is plausibly complete: big enough and starting with its format's header."""
    magic = IMAGE_MAGIC.get(path.suffix.lower())
    try:
        if path.stat().st_size < 1024:
            return False
        if magic is None:
            return True
        with path.open("rb") as f:
            return f.read(len(magic)) == magic
    except OSError:
        return False


def find_leftovers(root: Path, recursive: bool, opts: dict, stop: threading.Event) -> tuple[list[Finding], str]:
    permanent = bool(opts.get("permanent"))
    out: list[Finding] = []
    for folder, subdirs, names in _walk(root, recursive, stop):
        files = [folder / n for n in names]
        mains, owned = _disc_mains(folder, names)
        compressed: dict[str, list[Path]] = {}
        for p in files:
            if p.suffix.lower() in COMPRESSED_EXTENSIONS:
                compressed.setdefault(p.stem.casefold(), []).append(p)
        for main in sorted(mains):
            if main.suffix.lower() not in SOURCE_EXTENSIONS:
                continue
            twins = [t for t in compressed.get(main.stem.casefold(), []) if _looks_converted(t)]
            if not twins:
                continue
            group = _disc_files(main)
            size = sum(file_size(f) for f in group)
            out.append(Finding(main.name, str(len(group)), f"Converted copy: {twins[0].name}  |  frees {fmt_size(size)}",
                               [(f.name, fmt_size(file_size(f)), "") for f in group],
                               _removal_ops(group, root, permanent), size=size))
        for p in files:
            if p.name.lower().endswith(".part"):
                out.append(Finding(p.name, "1", "Leftover from an interrupted run", [],
                                   _removal_ops([p], root, permanent), size=file_size(p)))
            elif p.suffix.lower() in (".bin", ".img", ".wav") and TRACK_RE.search(p.stem) and p not in owned:
                out.append(Finding(p.name, "1", "Track file that no .cue in this folder uses", [],
                                   _removal_ops([p], root, permanent), checked=False, size=file_size(p)))
        if folder != root and not names and not subdirs:
            out.append(Finding(_rel(folder, root), "", "Empty folder", [], [("rmdir", str(folder))]))
    out.sort(key=lambda f: f.title.lower())
    return out, ""


# ---- Tool 3: duplicate games ---------------------------------------------------------------------------------
MATCH_MODES = ("Same name", "Same title, ignoring region / version tags", "Same contents (size, then checksum)")


# Game files that aren't disc images (cartridge ROMs, PC games, archives). Save files and patches are left out on purpose.
OTHER_GAME_EXTENSIONS = frozenset({
    ".gba", ".gbc", ".gb", ".nes", ".fds", ".sfc", ".smc", ".n64", ".z64", ".v64", ".nds", ".dsi", ".3ds", ".cia",
    ".gen", ".md", ".smd", ".sms", ".gg", ".pce", ".a26", ".a78", ".lnx", ".ws", ".wsc", ".ngp", ".ngc", ".vb",
    ".xci", ".nsp", ".wad", ".dol", ".elf", ".exe", ".zip", ".7z", ".rar"})


def _duplicate_candidates(root: Path, recursive: bool, stop: threading.Event, other: bool) -> list[Path]:
    found: list[Path] = []
    for folder, _subdirs, names in _walk(root, recursive, stop):
        found += _disc_mains(folder, names)[0]
        if other:
            found += [folder / n for n in names if Path(n).suffix.lower() in OTHER_GAME_EXTENSIONS]
    return found


def find_duplicates(root: Path, recursive: bool, opts: dict, stop: threading.Event) -> tuple[list[Finding], str]:
    policy = opts.get("keep") or DUPE_POLICIES[0]
    permanent = bool(opts.get("permanent"))
    mode = opts.get("match") or MATCH_MODES[0]
    other = bool(opts.get("other_files"))
    note = opts.get("_note") or (lambda _text: None)  # the page's live status line, when there is one
    candidates = _duplicate_candidates(root, recursive, stop, other)

    skipped = 0
    groups: dict[str, list[Path]] = {}
    if mode == MATCH_MODES[2]:
        singles = [p for p in candidates if p.suffix.lower() not in (".cue", ".gdi")]  # a sheet doesn't hold the data
        skipped = len(candidates) - len(singles)
        note(f"Looking at the sizes of {len(singles)} file(s)...")
        for n, members in enumerate(find_same_content(singles, stop, note)):
            groups[f"#{n}"] = members
    else:
        for p in candidates:
            key = re.sub(r"\s+", " ", p.stem).strip().casefold()
            if mode == MATCH_MODES[1]:
                core, number = title_core(p.stem)
                key = f"{core} #{number}" if number else core  # Disc 1 and Disc 2 are not copies of each other
            groups.setdefault(key, []).append(p)

    out: list[Finding] = []
    for _key, members in sorted(groups.items()):
        if len(members) < 2:
            continue
        discs_only = all(m.suffix.lower() in ORGANIZE_EXTENSIONS for m in members)
        if mode == MATCH_MODES[0] and discs_only and len({m.parent for m in members}) < 2:
            continue  # copies side by side in one folder are the Clean up tool's job
        files = {m: _disc_files(m) for m in members}
        sizes = {m: sum(file_size(f) for f in files[m]) for m in members}

        def rank(p: Path) -> tuple:
            if policy == "Keep the newest":
                return (_mtime(p),)
            if policy == "Keep the largest":
                return (sizes[p],)
            if policy == "Keep the smallest":
                return (-sizes[p],)
            return (p.suffix.lower() in COMPRESSED_EXTENSIONS, _mtime(p))

        keeper = max(members, key=rank)
        losers = [m for m in members if m is not keeper]
        freed = sum(sizes[m] for m in losers)
        kids = [(_rel(m, root), fmt_size(sizes[m]), "KEEP" if m is keeper else "Remove")
                for m in [keeper, *sorted(losers)]]
        ops = _removal_ops([f for m in losers for f in files[m]], root, permanent)
        if mode == MATCH_MODES[2]:
            why, ticked = "identical contents", True  # proven by checksum, whatever the files are called
        elif mode == MATCH_MODES[1]:
            why, ticked = "similar titles - check these are the same release", False
        elif not discs_only:
            why, ticked = "same name, not necessarily the same file - check", False
        else:
            why, ticked = "", True
        extra = f"  |  {why}" if why else ""
        out.append(Finding(keeper.stem, str(len(members)), f"Keeping {_rel(keeper, root)}  |  frees {fmt_size(freed)}{extra}",
                           kids, ops, checked=ticked, size=freed))
    how = {MATCH_MODES[0]: "matched by name, not by contents",
           MATCH_MODES[1]: "matched by title; these are left unticked for you to check",
           MATCH_MODES[2]: "matched by contents" + (f"; {skipped} .cue / .gdi sheet(s) skipped" if skipped else "")}[mode]
    return out, how + ("; ROMs and other game files included, left unticked" if other and mode != MATCH_MODES[2] else "")


# ---- Tool 4: playlist repair ---------------------------------------------------------------------------------
def find_playlist_fixes(root: Path, recursive: bool, opts: dict, stop: threading.Event) -> tuple[list[Finding], str]:
    add_missing = bool(opts.get("add_missing", True))
    by_name: dict[str, list[Path]] = {}
    by_stem: dict[str, list[Path]] = {}
    in_folder: dict[Path, list[Path]] = {}
    lists: list[Path] = []
    for folder, _subdirs, names in _walk(root, recursive, stop):
        for n in names:
            p = folder / n
            if p.suffix.lower() == ".m3u":
                lists.append(p)
            elif p.suffix.lower() in ORGANIZE_EXTENSIONS:
                by_name.setdefault(n.casefold(), []).append(p)
                by_stem.setdefault(p.stem.casefold(), []).append(p)
                in_folder.setdefault(folder, []).append(p)

    def best(cands: list[Path]) -> Path | None:
        if len(cands) == 1:
            return cands[0]
        compressed = [c for c in cands if c.suffix.lower() in COMPRESSED_EXTENSIONS]
        return compressed[0] if len(compressed) == 1 else None

    out: list[Finding] = []
    for m3u in sorted(lists, key=lambda p: str(p).lower()):
        try:
            lines = _read_text(m3u).split("\n")
        except OSError:
            continue
        comments = [ln.strip() for ln in lines if ln.strip().startswith("#")]
        entries = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
        if not entries:
            continue
        kids: list[tuple[str, str, str]] = []
        final: list[str] = []
        seen: set[str] = set()
        changed, unresolved = False, 0
        for entry in entries:
            p = Path(entry.replace("\\", "/"))
            path = p if p.is_absolute() else m3u.parent / p
            if path.exists():
                new, note, target = entry, "OK", path
                if p.is_absolute():
                    with contextlib.suppress(ValueError):
                        new = path.relative_to(m3u.parent).as_posix()
                        note = "Made relative"
            else:
                same_folder = [c for c in by_stem.get(path.stem.casefold(), []) if c.parent == path.parent]
                target = (best(same_folder) or best(by_name.get(path.name.casefold(), []))
                          or best(by_stem.get(path.stem.casefold(), [])))
                if target:
                    new = os.path.relpath(target, m3u.parent).replace("\\", "/")
                    note = f"Now {new}"
                else:
                    new, note = entry, "Missing"
                    unresolved += 1
            key = str(target).casefold() if target else _m3u_key(entry)
            if key in seen:
                kids.append((entry, "", "Duplicate - removed"))
                changed = True
                continue
            seen.add(key)
            changed |= new != entry
            final.append(new)
            kids.append((entry, "", note))
        if add_missing:
            parsed = [parse_disc(Path(x).stem) for x in final]
            if parsed and all(parsed) and len({q[0].casefold() for q in parsed}) == 1:
                base, have, added = parsed[0][0].casefold(), {q[1] for q in parsed}, False
                for cand in sorted(in_folder.get(m3u.parent, []),
                                   key=lambda c: (c.suffix.lower() not in COMPRESSED_EXTENSIONS, c.name.lower())):
                    pc = parse_disc(cand.stem)
                    if pc and pc[0].casefold() == base and pc[1] not in have:
                        final.append(cand.name)
                        have.add(pc[1])
                        kids.append((cand.name, "", "Added (was missing from the playlist)"))
                        added = changed = True
                if added:
                    final.sort(key=lambda x: parse_disc(Path(x).stem)[1])
        title = _rel(m3u, root)
        if changed:
            note = "Will be repaired" + (f"  |  {unresolved} still missing" if unresolved else "")
            out.append(Finding(title, str(len(final)), note, kids,
                               [("write", str(m3u), "\n".join([*comments, *final]) + "\n")]))
        elif unresolved:
            out.append(Finding(title, str(len(final)), "", kids, [],
                               problem=f"{unresolved} entr{'y' if unresolved == 1 else 'ies'} point to files that "
                                       "couldn't be found anywhere in this folder"))
    return out, ""


# ---- Tool 5: tidy filenames ----------------------------------------------------------------------------------
def tidy_name(stem: str, opts: dict) -> str:
    """The tidied form of a file name (without its extension); stem itself when nothing needs changing."""
    name = stem
    if opts.get("spaces", True):
        name = re.sub(r"\s+", " ", name.replace("_", " ")).strip()
    if opts.get("discs", True) and (matches := list(DISC_RE.finditer(name))):
        m = matches[-1]
        total = m[2] or m[4]
        canon = f"(Disc {int(m[1] or m[3])}" + (f" of {int(total)})" if total else ")")
        if m.group(0) != canon:
            name = f"{name[:m.start()]}{canon}{name[m.end():]}"
    if opts.get("articles", False) and (m := ARTICLE_RE.match(name)):
        name = f"{m[2]} {m[1]}{m[3]}"
    if opts.get("tags", False):
        name = BRACKET_TAG_RE.sub(lambda m: m.group(0) if DISC_TAG_RE.match(m.group(0)) else "", name)
    if name == stem:
        return stem
    return re.sub(r"\s+", " ", name).strip(" -_.,") or stem


def find_tidy_renames(root: Path, recursive: bool, opts: dict, stop: threading.Event) -> tuple[list[Finding], str]:
    out: list[Finding] = []
    claimed: dict[tuple[Path, str], str] = {}
    skipped = 0
    for folder, _subdirs, names in _walk(root, recursive, stop):
        mains = sorted(_disc_mains(folder, names)[0])
        if not mains:
            continue
        taken = {n.casefold() for n in names}
        listed: dict[Path, set[str]] = {}
        for n in names:
            if n.lower().endswith(".m3u"):
                with contextlib.suppress(OSError):
                    listed[folder / n] = {_m3u_key(ln) for ln in _read_text(folder / n).split("\n")}
        for main in mains:
            new_stem = tidy_name(main.stem, opts)
            if new_stem.casefold() == main.stem.casefold():
                continue  # unchanged, or only the letter case differs (not worth the rename)
            if main.suffix.lower() not in SINGLE_FILE_EXTENSIONS:
                skipped += 1
                continue
            files = [main] + [folder / n for n in names if Path(n).stem == main.stem
                              and Path(n).suffix.lower() in SIDECAR_EXTENSIONS]
            ops: list[tuple] = []
            kids: list[tuple[str, str, str]] = []
            problem = ""
            for f in files:
                dest_name = new_stem + f.suffix
                if dest_name.casefold() in taken:
                    problem = problem or f"'{dest_name}' already exists"
                if (other := claimed.get((folder, dest_name.casefold()))) is not None:
                    problem = problem or f"Same new name as {other}"
                claimed[(folder, dest_name.casefold())] = f.name
                ops.append(("move", str(f), str(folder / dest_name)))
                kids.append((f"{f.name}  ->  {dest_name}", "", ""))
            for m3u, keys in listed.items():
                if _m3u_key(main.name) in keys:
                    ops.append(("replace_line", str(m3u), main.name, new_stem + main.suffix))
                    kids.append((f"Playlist {m3u.name} is updated to match", "", ""))
            out.append(Finding(main.name, str(len(files)), f"->  {new_stem + main.suffix}", kids, ops, problem))
    out.sort(key=lambda f: f.title.lower())
    return out, (f"{skipped} .cue / .gdi / .toc set(s) skipped (they name their track files inside)" if skipped else "")


# ---- registry: the Organize tools and what the UI needs to know about each --------------------------------
def _confirm_removal(picks: list[Finding], opts: dict) -> str:
    how = ("PERMANENTLY DELETED (this can't be undone)" if opts.get("permanent")
           else f"moved into a '{HOLDING_DIR}' folder (Undo Last Run restores them)")
    return f"{len(picks)} item(s), {fmt_size(sum(f.size for f in picks))}, will be {how}.\n\nContinue?"


@dataclass(frozen=True, slots=True)
class OrgTool:
    key: str
    nav: str  # sidebar label
    title: str
    blurb: str
    headings: tuple[str, str]  # the two tree columns after the tick box
    apply_label: str
    empty: str  # status text when a scan finds nothing
    scan: Callable[[Path, bool, dict, threading.Event], tuple[list[Finding], str]]
    options: tuple[tuple, ...] = ()  # (name, "check" | "choice", label, default[, choices])
    confirm: Callable[[list[Finding], dict], str] | None = None


_PERMANENT = ("permanent", "check", f"Delete permanently instead of moving to a '{HOLDING_DIR}' folder "
                                     "(can't be undone)", False)


ORG_TOOLS: dict[str, OrgTool] = {t.key: t for t in (
    OrgTool("cleanup", "Clean up", "Clean up leftovers",
            "Find source files that already have a converted copy, plus partial files, orphaned track files and "
            "empty folders.", ("Leftover", "Files"), "Clean Up Selected", "Nothing to clean up.",
            find_leftovers, (_PERMANENT,), _confirm_removal),
    OrgTool("dupes", "Duplicates", "Duplicate games",
            "Find the same game stored in more than one place and keep one copy - matched by name, by title "
            "(ignoring region and version tags) or by what is actually inside the files.",
            ("Game", "Copies"), "Remove Selected Duplicates", "No duplicates found.", find_duplicates,
            (("match", "choice", "Match by:", MATCH_MODES[0], MATCH_MODES),
             ("keep", "choice", "Keep:", DUPE_POLICIES[0], DUPE_POLICIES),
             ("other_files", "check", "Also look at ROMs and other game files (.gba, .nes, .exe, .zip ...), not just disc images",
              False),
             _PERMANENT), _confirm_removal),
    OrgTool("tidy", "Tidy filenames", "Tidy filenames",
            "Normalise disc markers and other filename quirks so games group and sort properly. Playlists are "
            "updated to match.", ("File", "Files"), "Rename Selected Files",
            "All filenames already look tidy.", find_tidy_renames,
            (("discs", "check", "Normalise disc markers: [CD2], (Disk 2), Disc 2  ->  (Disc 2)", True),
             ("spaces", "check", "Tidy spacing and turn underscores into spaces", True),
             ("articles", "check", "Move a trailing article to the front: Legend of Zelda, The  ->  The Legend of Zelda",
              False),
             ("tags", "check", "Remove bracketed dump tags such as [!] or [b1]", False))),
    OrgTool("multi", "Multi-disc games", "Multi-disc games",
            "Put the discs of each multi-disc game in a folder named after it, with an .m3u playlist.",
            ("Game  ->  folder", "Discs"), "Organize Selected Games",
            "No multi-disc games found that still need organizing.", find_multidisc,
            (("loose", "check", "Loose matching: also pair discs whose names are only similar, such as a different "
                                "subtitle (shown unticked for you to check)", False),)),
    OrgTool("playlists", "Playlist repair", "Playlist repair",
            "Fix .m3u playlists that point to renamed, converted or moved discs, and add discs they're missing.",
            ("Playlist", "Entries"), "Repair Selected Playlists", "All playlists look fine.", find_playlist_fixes,
            (("add_missing", "check", "Add discs that are in the folder but missing from the playlist", True),)),
)}
