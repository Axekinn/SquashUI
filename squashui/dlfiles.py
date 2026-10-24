"""What a download leaves on disk: finding it, revealing it, deleting it, and keeping the list's row numbers
straight when rows are taken out of it. No Qt in here, so the Download page's right-click menu stays thin."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

from .downloads import DownloadRunner, RemoteFile


PART_SUFFIX = ".part"  # what a web download is called until it is whole


def per_item_for(rf: RemoteFile, folder_per_item: bool) -> bool:
    """The 'Folder per item' option only applies to web files; torrents keep their own folder layout."""
    return folder_per_item and not rf.url.startswith("torrent:")


def files_on_disk(rf: RemoteFile, dest: Path, per_item: bool) -> list[Path]:
    """The finished file and/or its partial download, whichever exist. A name that would escape `dest` gives []."""
    try:
        final = DownloadRunner.locate(rf, dest, per_item)
    except ValueError:
        return []
    found = []
    for path in (final, final.with_name(final.name + PART_SUFFIX)):
        try:
            if path.is_file():
                found.append(path)
        except OSError:
            pass
    return found


def nearest_folder(rf: RemoteFile, dest: Path, per_item: bool) -> Path | None:
    """The folder this file is saved in, or the closest one above it that exists (never above `dest`)."""
    try:
        folder = DownloadRunner.locate(rf, dest, per_item).parent
    except ValueError:
        return None
    root = dest.resolve()
    while True:
        try:
            if folder.is_dir():
                return folder
        except OSError:
            pass
        if folder.resolve() == root or not folder.resolve().is_relative_to(root):
            return None
        folder = folder.parent


def explorer_select_command(path: Path) -> list[str] | None:
    """Command that opens Explorer with this file highlighted. Windows only; elsewhere the caller opens the folder."""
    return ["explorer", "/select,", str(path)] if os.name == "nt" and path.exists() else None


def magnet_for(rf: RemoteFile) -> str | None:
    """A magnet link for a torrent file entry ('torrent:<infohash>:<file number>'); None for anything else."""
    parts = rf.url.split(":")
    return f"magnet:?xt=urn:btih:{parts[1]}" if len(parts) == 3 and parts[0] == "torrent" and parts[1] else None


def compact_plan(count: int, removed: set[int]) -> list[int | None]:
    """Old row number -> new row number once `removed` rows are taken out (None for the removed rows themselves)."""
    plan: list[int | None] = []
    new = 0
    for i in range(count):
        if i in removed:
            plan.append(None)
        else:
            plan.append(new)
            new += 1
    return plan


def delete_files(paths: list[Path], dest: Path, patience: float = 3.0) -> tuple[int, list[str]]:
    """Delete these files, then any folders that leaves empty. Returns (files deleted, problems).

    Never touches anything outside `dest`, and never `dest` itself. Windows refuses to delete a file something still
    has open (a download that is only just stopping, an antivirus scan), so a refusal is retried for `patience`
    seconds before it is reported."""
    root = dest.resolve()
    deadline = time.monotonic() + patience
    deleted, problems, parents = 0, [], set()
    for path in paths:
        try:
            inside = path.resolve().is_relative_to(root) and path.resolve() != root
        except OSError:
            inside = False
        if not inside:
            problems.append(f"{path.name}: outside the save folder, left alone")
            continue
        while True:
            try:
                path.unlink()
                deleted += 1
                parents.add(path.parent)
                break
            except FileNotFoundError:
                break  # already gone: nothing to do
            except PermissionError as exc:
                if time.monotonic() >= deadline:
                    problems.append(f"{path.name}: {exc.strerror or 'in use by another program'}")
                    break
                time.sleep(0.5)
            except OSError as exc:
                problems.append(f"{path.name}: {exc.strerror or exc}")
                break
    for folder in sorted(parents, key=lambda p: len(p.parts), reverse=True):  # deepest first
        while folder.resolve() != root and folder.resolve().is_relative_to(root):
            try:
                folder.rmdir()  # only succeeds on an empty folder
            except OSError:
                break
            folder = folder.parent
    return deleted, problems


def wait_until(done: Callable[[], bool], patience: float, step: float = 0.2) -> bool:
    """Poll until done() is true; False if `patience` seconds went by first."""
    end = time.monotonic() + patience
    while not done():
        if time.monotonic() >= end:
            return False
        time.sleep(step)
    return True
