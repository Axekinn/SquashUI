"""Turning what the person pasted (archive.org item, directory listing, 1fichier, magnet, .torrent) into
a list of downloadable files."""
from __future__ import annotations

import http.client
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from .downloads import (
    http_open, LinkError, list_1fichier, list_archive_item,
    list_open_directory, parse_archive_url, site_for,
)
from .torbox import torbox_list_magnet
from .torrents import (
    ENGINE, import_libtorrent, is_torrent_token, load_torrent_settings,
    NEEDS_LIBTORRENT, torrent_files_from_bytes,
)


def resolve_links(text: str) -> tuple[list[RemoteFile], list[str]]:
    """Turn pasted text (any number of links) into one de-duplicated file list plus per-link problems."""
    files, errors, seen = [], [], set()
    for token in text.split():
        try:
            if is_torrent_token(token):
                found = list_torrent(token)
                for f in found:
                    if f.url not in seen:
                        seen.add(f.url)
                        files.append(f)
                continue
            parsed = parse_archive_url(token)
            full = token if "://" in token else "https://" + token
            if parsed:
                found = list_archive_item(*parsed)
            elif site_for(full) == "1fichier":
                found = list_1fichier(full)
            else:
                found = list_open_directory(full)
        except LinkError as exc:
            errors.append(str(exc))
            continue
        for f in found:
            if f.url not in seen:
                seen.add(f.url)
                files.append(f)
    return files, errors


def probe_sizes(files: list[RemoteFile], on_size: Callable[[int, int], None], workers: int = 8) -> None:
    """HEAD every file whose size the listing didn't give, reporting each as it arrives."""
    def head(i: int) -> None:
        if files[i].url.startswith("torrent:") or site_for(files[i].url) == "1fichier":
            return  # sizes already known, and these aren't plain web files
        try:
            with http_open(files[i].url, method="HEAD", timeout=15) as resp:
                n = resp.headers.get("Content-Length", "")
                if n.isdigit():
                    on_size(i, int(n))
        except (OSError, http.client.HTTPException) as exc:
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()

    todo = [i for i, f in enumerate(files) if f.size is None]
    with ThreadPoolExecutor(workers, thread_name_prefix="probe") as pool:
        list(pool.map(head, todo))


def list_torrent(token: str) -> list[RemoteFile]:
    token = token.strip("\"'")
    is_magnet = token.lower().startswith("magnet:")
    if is_magnet and "xt=urn:bt" not in token.lower():
        raise LinkError("That magnet link isn't valid (no torrent id in it).")
    if is_magnet and (cached := torbox_list_magnet(token)):  # cached on TorBox: no swarm, no VPN needed
        return cached
    lt = import_libtorrent()
    if lt is None:
        raise LinkError(NEEDS_LIBTORRENT.replace("\n", " "))
    if is_magnet:
        ENGINE.ensure(load_torrent_settings())  # the VPN check happens before anything touches the network
        data = ENGINE.fetch_metadata(token)
    else:
        path = Path(urllib.request.url2pathname(urllib.parse.urlsplit(token).path)) if token.lower().startswith("file:") \
            else Path(token)
        try:
            if path.stat().st_size > 20 << 20:
                raise LinkError(f"{path.name} is too big to be a .torrent file.")
            data = path.read_bytes()
        except OSError as exc:
            raise LinkError(f"Can't read {path.name}: {exc.strerror or exc}") from None
    return torrent_files_from_bytes(lt, data)
