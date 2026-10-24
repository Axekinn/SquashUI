"""TorBox: reuse a cached copy of a torrent instead of joining the swarm."""
from __future__ import annotations

import base64
import contextlib
import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .constants import APP_DIR
from .downloads import _OPENER, LinkError, LOGINS, RemoteFile, USER_AGENT
from .torrents import ENGINE


# A torrent TorBox already has cached comes down over plain HTTPS from its servers: fast, and nobody in a swarm ever
# sees your IP address. Only what is NOT cached falls back to libtorrent (with the VPN rules above).
TORBOX_API = "https://api.torbox.app/v1/api"


def torbox_key() -> str:
    return str((LOGINS.get("torbox") or {}).get("apikey", "")).strip()


def _torbox_call(path: str, params: dict | None = None, form: dict | None = None, timeout: float = 30) -> dict:
    """One TorBox API call -> its parsed JSON reply. Every failure is a LinkError with a readable reason."""
    key = torbox_key()
    if not key:
        raise LinkError("No TorBox API key (Settings > Downloads & accounts > Logins).")
    url = f"{TORBOX_API}/{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    headers, body = {"User-Agent": USER_AGENT, "Authorization": f"Bearer {key}", "Accept": "application/json"}, None
    if form is not None:  # createtorrent takes multipart/form-data
        boundary = "----squashui" + uuid.uuid4().hex
        body = b"".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode("utf-8")
                        for k, v in form.items()) + f"--{boundary}--\r\n".encode("ascii")
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST" if form is not None else "GET")
    try:
        with _OPENER.open(request, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        with contextlib.suppress(Exception):
            detail = str(json.loads(exc.read() or b"{}").get("detail") or "")
        exc.close()
        if exc.code in (401, 403):
            raise LinkError("TorBox rejected the API key - check it under Logins.") from None
        raise LinkError(f"TorBox: HTTP {exc.code}" + (f" - {detail}" if detail else "")) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise LinkError(f"TorBox unreachable: {getattr(exc, 'reason', exc)}") from None
    try:
        reply = json.loads(raw)
    except ValueError:
        raise LinkError("TorBox sent a reply that couldn't be read.") from None
    if not isinstance(reply, dict):
        raise LinkError("TorBox sent a reply that couldn't be read.")
    if reply.get("success") is False:
        raise LinkError(f"TorBox: {reply.get('detail') or reply.get('error') or 'the request failed'}")
    return reply


def magnet_hash(magnet: str) -> str:
    """The 40-character hex info-hash of a magnet link ('' if it hasn't a usable one)."""
    m = re.search(r"xt=urn:btih:([^&]+)", magnet, re.I)
    if not m:
        return ""
    value = m.group(1)
    if re.fullmatch(r"[0-9a-fA-F]{40}", value):
        return value.lower()
    if re.fullmatch(r"[A-Za-z2-7]{32}", value):  # the base-32 spelling some magnets use
        with contextlib.suppress(ValueError):
            return base64.b32decode(value.upper()).hex()
    return ""


def torbox_cached(hashes: list[str], list_files: bool = False) -> dict[str, dict]:
    """{info-hash: details} for the torrents TorBox already has cached; the others are simply absent."""
    found: dict[str, dict] = {}
    for i in range(0, len(hashes), 50):  # the endpoint takes about 100 hashes per query
        reply = _torbox_call("torrents/checkcached", {"hash": ",".join(hashes[i:i + 50]), "format": "object",
                                                      "list_files": "true" if list_files else "false"})
        data = reply.get("data")
        if isinstance(data, dict):
            for ih, info in data.items():
                if info:
                    found[str(ih).lower()] = info if isinstance(info, dict) else {}
    return found


def torbox_list_magnet(magnet: str) -> list[RemoteFile] | None:
    """The file list of a magnet that TorBox has cached, or None (no key, not cached, any problem) - the caller
    then asks the swarm instead."""
    ih = magnet_hash(magnet)
    if not ih or not torbox_key():
        return None
    ENGINE.note("Checking TorBox for a cached copy...")
    try:
        info = torbox_cached([ih], True).get(ih)
    except LinkError:
        return None
    raw = _flatten_files(info.get("files") if info else None)
    if not raw:
        return None
    with contextlib.suppress(OSError, TypeError, ValueError):  # for diagnosing odd replies
        (APP_DIR / "squashui_torbox_debug.json").write_text(json.dumps(info)[:200000], encoding="utf-8")
    group = str(info.get("name") or ih)
    names = [str(f.get("name") or "").replace("\\", "/").strip("/") for f in raw]
    prefix = len(names) > 1 and not all(n.startswith(group + "/") for n in names)  # a multi-file torrent sits in a folder
    files: list[RemoteFile] = []
    for i, f in enumerate(raw):
        name = names[i]
        if not name:
            continue
        name = f"{group}/{name}" if prefix else name
        parts = [seg for seg in name.split("/") if seg]
        if not parts or ".." in parts or ":" in parts[0] or ".pad" in parts:
            continue
        size = f.get("size")
        files.append(RemoteFile("/".join(parts), f"torrent:{ih}:{i}", int(size) if isinstance(size, (int, float)) else None, group,
                                 source="TorBox"))
    ENGINE.note(f"TorBox lists {len(raw)} entries, {len(files)} usable.")
    return files or None


def _flatten_files(raw, parent: str = "") -> list[dict]:
    """TorBox file entries as a flat list of {'name', 'size'}; copes with a dict of entries or folders holding children."""
    if isinstance(raw, dict):
        raw = list(raw.values())
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for f in raw:
        if not isinstance(f, dict):
            continue
        name = str(f.get("name") or "").replace("\\", "/").strip("/")
        kids = next((f[k] for k in ("children", "files", "contents") if isinstance(f.get(k), (list, dict))), None)
        full = f"{parent}/{name}" if parent and name else (name or parent)
        if kids:
            out.extend(_flatten_files(kids, full))
        else:
            out.append({**f, "name": full})
    return out


def torbox_add(ih: str, name: str) -> int:
    """Put a cached torrent on the account (instant when it is cached) and return TorBox's id for it."""
    magnet = f"magnet:?xt=urn:btih:{ih}" + (f"&dn={urllib.parse.quote(name)}" if name else "")
    reply = _torbox_call("torrents/createtorrent", form={"magnet": magnet, "seed": "3", "allow_zip": "false",
                                                         "as_queued": "false", "add_only_if_cached": "true"})
    data = reply.get("data")
    tid = (data.get("torrent_id") or data.get("id")) if isinstance(data, dict) else None
    if not isinstance(tid, int):
        raise LinkError("TorBox didn't accept the torrent (it may no longer be cached).")
    return tid


def torbox_files(torrent_id: int) -> list[dict]:
    """The files TorBox lists for one of the account's torrents: [{'id', 'name', 'size', ...}]."""
    reply = _torbox_call("torrents/mylist", {"id": torrent_id, "bypass_cache": "true"})
    data = reply.get("data")
    if isinstance(data, list):
        data = next((d for d in data if isinstance(d, dict) and d.get("id") == torrent_id), None)
    files = data.get("files") if isinstance(data, dict) else None
    return [f for f in files if isinstance(f, dict) and isinstance(f.get("id"), int)] if isinstance(files, list) else []


def torbox_link(torrent_id: int, file_id: int) -> str:
    """A short-lived direct HTTPS link to one file of a TorBox torrent."""
    reply = _torbox_call("torrents/requestdl", {"token": torbox_key(), "torrent_id": torrent_id, "file_id": file_id,
                                                "zip_link": "false"})
    url = reply.get("data")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise LinkError("TorBox gave no download link for this file.")
    return url


def _torbox_match(rf: RemoteFile, tb_files: list[dict]) -> dict | None:
    """The TorBox file that is this listing entry: by path, then by path suffix, then by size and file name."""
    def norm(f: dict) -> str:
        return str(f.get("name") or "").replace("\\", "/").strip("/").lower()

    want = rf.name.replace("\\", "/").strip("/").lower()
    for f in tb_files:
        if norm(f) == want:
            return f
    for f in tb_files:
        n = norm(f)
        if n and (n.endswith("/" + want) or want.endswith("/" + n)):
            return f
    base = want.rsplit("/", 1)[-1]
    same = [f for f in tb_files if rf.size is not None and f.get("size") == rf.size and norm(f).rsplit("/", 1)[-1] == base]
    return same[0] if len(same) == 1 else None
