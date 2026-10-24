"""Torrents (optional libtorrent), built so traffic can only leave through the VPN adapter."""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from .constants import APP_DIR
from .downloads import LinkError, RemoteFile


TORRENT_FILE = APP_DIR / "squashui_torrent_settings.json"


TORRENT_DIR = APP_DIR / "squashui_torrents"  # a copy of each torrent's details, so a restored list can resume


TORRENT_DEFAULTS = {"adapter": "", "require_vpn": True, "dht": True}


NEEDS_LIBTORRENT = ("Torrent support needs the libtorrent package. Open a command prompt, run:\n\n"
                    "    pip install libtorrent\n\nthen restart this app.")


_VPN_WORDS = ("vpn", "wireguard", "wintun", "openvpn", "tap-windows", "nordlynx", "mullvad", "proton",
              "windscribe", "surfshark", "cyberghost", "ivpn", "private internet")


class TorrentSafetyError(LinkError):
    """Torrent traffic could not be made safe (no VPN adapter, VPN down, libtorrent missing...)."""


def import_libtorrent():
    try:
        import libtorrent  # type: ignore[import-not-found]
    except ImportError:
        return None
    return libtorrent


def looks_like_vpn(*texts: str) -> bool:
    blob = " ".join(texts).lower()
    return any(w in blob for w in _VPN_WORDS) or bool(re.search(r"\b(tun|tap|utun|wg|ppp)\d*\b", blob))


def is_torrent_token(token: str) -> bool:
    low = token.lower().strip("\"'")
    return low.startswith("magnet:") or low.endswith(".torrent")


def load_torrent_settings() -> dict:
    out = dict(TORRENT_DEFAULTS)
    try:
        data = json.loads(TORRENT_FILE.read_text(encoding="utf-8"))
        for key, default in TORRENT_DEFAULTS.items():
            if isinstance(data.get(key), type(default)):
                out[key] = data[key]
    except (OSError, ValueError, AttributeError):
        pass
    return out


def save_torrent_settings(settings: dict) -> None:
    with contextlib.suppress(OSError):
        TORRENT_FILE.write_text(json.dumps({k: settings.get(k, v) for k, v in TORRENT_DEFAULTS.items()}), encoding="utf-8")


def list_adapters() -> list[dict]:
    """IPv4 network adapters that have a usable address: [{'name', 'ip', 'desc', 'up', 'vpn'}]."""
    rows: list[dict] = []
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        if os.name == "nt":  # PowerShell's JSON output is the same in every Windows language
            script = ("Get-NetIPAddress -AddressFamily IPv4 | ForEach-Object { $a = Get-NetAdapter -InterfaceIndex "
                      "$_.InterfaceIndex -ErrorAction SilentlyContinue; [pscustomobject]@{Name=$_.InterfaceAlias; "
                      "IP=$_.IPAddress; Desc=$a.InterfaceDescription; Status=[string]$a.Status} } | ConvertTo-Json -Compress")
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                 capture_output=True, text=True, timeout=25, creationflags=flags)
            data = json.loads(out.stdout or "[]")
            data = [data] if isinstance(data, dict) else data
            found = [(str(d.get("Name", "")), str(d.get("IP", "")), str(d.get("Desc") or ""),
                      str(d.get("Status", "")).lower() in ("up", "")) for d in data if isinstance(d, dict)]
        else:
            out = subprocess.run(["ip", "-j", "addr"], capture_output=True, text=True, timeout=10)
            found = [(d.get("ifname", ""), a.get("local", ""), "", str(d.get("operstate", "")).upper() in ("UP", "UNKNOWN"))
                     for d in json.loads(out.stdout or "[]") for a in d.get("addr_info", []) if a.get("family") == "inet"]
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    for name, ip, desc, up in found:
        if ip and not ip.startswith(("127.", "169.254.")):
            rows.append({"name": name, "ip": ip, "desc": desc, "up": up, "vpn": looks_like_vpn(name, desc)})
    return rows


def ip_is_local(ip: str) -> bool:
    """True while this address still belongs to a network adapter of this computer (cheap; used as the kill switch)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind((ip, 0))
        return True
    except OSError:
        return False


def torrent_gate(settings: dict) -> tuple[str, str]:
    """(address to bind to, description). Raises TorrentSafetyError when the VPN requirement isn't met."""
    if not settings.get("require_vpn", True):
        return "", "no VPN binding - peers can see your real IP"
    name = settings.get("adapter", "")
    if not name:
        raise TorrentSafetyError("Choose your VPN adapter first (Torrent privacy...).")
    for adapter in list_adapters():
        if adapter["name"] == name and adapter["up"]:
            return adapter["ip"], f"{name} ({adapter['ip']})"
    raise TorrentSafetyError(f"The VPN adapter '{name}' isn't connected. Connect your VPN and try again - "
                             "nothing was sent.")


class TorrentEngine:
    """One shared libtorrent session. Every connection is bound to the chosen adapter's address, so if the VPN
    disappears there is simply no route for torrent traffic to leak through."""

    def __init__(self) -> None:
        self.lt = None
        self.ses = None
        self.lock = threading.Lock()
        self.bound_ip, self.label = "", ""
        self.note: Callable[[str], None] = lambda _t: None

    @staticmethod
    def _pack(settings: dict, ip: str) -> dict:
        pack = {
            "user_agent": "",  # don't announce which client this is
            "anonymous_mode": True,  # random peer id, no client version, no IP in tracker announces
            "enable_upnp": False, "enable_natpmp": False,  # never ask the router to open ports
            "enable_lsd": False,  # no announcing on the local network
            "enable_dht": bool(settings.get("dht", True)),
        }
        if ip:
            pack["listen_interfaces"] = f"{ip}:0"  # IPv4 on the VPN adapter only (no IPv6 side door)
            pack["outgoing_interfaces"] = ip
        return pack

    def ensure(self, settings: dict) -> None:
        """Create the session (or re-point it after a VPN reconnect). Raises TorrentSafetyError if unsafe."""
        lt = import_libtorrent()
        if lt is None:
            raise TorrentSafetyError(NEEDS_LIBTORRENT.replace("\n", " "))
        ip, label = torrent_gate(settings)
        with self.lock:
            pack = self._pack(settings, ip)
            if self.ses is None:
                self.ses = lt.session(pack)
            else:
                self.ses.apply_settings(pack)
            self.lt, self.bound_ip, self.label = lt, ip, label

    def fetch_metadata(self, uri: str, timeout: float = 120) -> bytes:
        """Join the swarm just long enough to learn the file list, then drop the torrent again."""
        lt, ses = self.lt, self.ses
        try:
            params = lt.parse_magnet_uri(uri)
        except (RuntimeError, ValueError):
            raise LinkError("That magnet link isn't valid.") from None
        scratch = Path(tempfile.mkdtemp(prefix="squashui_meta_"))
        params.save_path = str(scratch)
        handle = ses.add_torrent(params)
        try:
            started = time.monotonic()
            while time.monotonic() - started < timeout:
                status = handle.status()
                if status.has_metadata and (info := handle.torrent_file()) is not None:
                    return lt.bencode(lt.create_torrent(info).generate())
                self.note(f"Fetching the file list from the swarm... {status.num_peers} peer(s), "
                          f"{int(time.monotonic() - started)}s")
                time.sleep(0.5)
            raise LinkError("Timed out waiting for the torrent's file list (no peers found - it may be dead).")
        finally:
            with contextlib.suppress(Exception):
                ses.remove_torrent(handle)
            shutil.rmtree(scratch, ignore_errors=True)


ENGINE = TorrentEngine()


def torrent_hash(info) -> str:
    for getter in (lambda: str(info.info_hashes().v1), lambda: str(info.info_hash()), lambda: str(info.info_hashes().v2)):
        try:
            value = getter()
        except (AttributeError, RuntimeError):
            continue
        if value.strip("0"):
            return value.lower()
    raise LinkError("Couldn't read this torrent's identity.")


def torrent_files_from_bytes(lt, data: bytes) -> list[RemoteFile]:
    try:
        info = lt.torrent_info(lt.bdecode(data))
    except (RuntimeError, ValueError, TypeError):
        raise LinkError("That isn't a valid torrent.") from None
    ih = torrent_hash(info)
    TORRENT_DIR.mkdir(parents=True, exist_ok=True)
    (TORRENT_DIR / f"{ih}.torrent").write_bytes(data)  # kept so a restored list can resume
    storage, group, files, skipped = info.files(), info.name(), [], 0
    for i in range(info.num_files()):
        path = storage.file_path(i).replace("\\", "/")
        parts = [seg for seg in path.split("/") if seg]
        if not parts or ".." in parts or path.startswith("/") or ":" in parts[0] or parts[0] == ".pad" or ".pad" in parts:
            skipped += 1
            continue  # padding files and anything trying to escape the save folder
        files.append(RemoteFile("/".join(parts), f"torrent:{ih}:{i}", int(storage.file_size(i)), group, source="Torrent"))
    if not files:
        raise LinkError("That torrent has no downloadable files.")
    ENGINE.note(f"Torrent lists {info.num_files()} entries, {len(files)} usable ({skipped} skipped).")
    return files
