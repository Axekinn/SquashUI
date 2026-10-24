from __future__ import annotations

import base64
import hashlib
import unittest
from unittest import mock

import squashui.torrents as torrents
from squashui.torbox import magnet_hash
from squashui.torrents import TorrentSafetyError, ip_is_local, is_torrent_token, looks_like_vpn, torrent_gate
from tests.helpers import TempDirTest

HEX = "0123456789abcdef0123456789abcdef01234567"


class TokenTests(unittest.TestCase):
    def test_what_counts_as_a_torrent(self):
        for token in ("magnet:?xt=urn:btih:" + HEX, "MAGNET:?xt=1", "file.torrent", '"quoted.TORRENT"', "'x.torrent'"):
            with self.subTest(token=token):
                self.assertTrue(is_torrent_token(token))
        for token in ("https://example.com/file.zip", "torrent.txt", "magnetic"):
            with self.subTest(token=token):
                self.assertFalse(is_torrent_token(token))


class MagnetTests(unittest.TestCase):
    def test_hex_hash(self):
        self.assertEqual(magnet_hash(f"magnet:?xt=urn:btih:{HEX.upper()}&dn=Name"), HEX)

    def test_base32_hash_is_converted(self):
        raw = bytes(range(20))
        self.assertEqual(magnet_hash("magnet:?xt=urn:btih:" + base64.b32encode(raw).decode()), raw.hex())

    def test_unusable_magnets(self):
        for magnet in ("magnet:?dn=NoHash", "magnet:?xt=urn:btih:tooshort", "not a magnet", ""):
            with self.subTest(magnet=magnet):
                self.assertEqual(magnet_hash(magnet), "")


class VpnDetectionTests(unittest.TestCase):
    def test_adapter_names(self):
        for text in ("NordLynx", "Mullvad VPN", "WireGuard Tunnel", "TAP-Windows Adapter V9", "tun0", "utun3", "wg0",
                     "ProtonVPN"):
            with self.subTest(text=text):
                self.assertTrue(looks_like_vpn(text))
        for text in ("Ethernet", "Wi-Fi", "Realtek PCIe GbE", "Intel(R) Wi-Fi 6 AX201", "Hyper-V Virtual Ethernet"):
            with self.subTest(text=text):
                self.assertFalse(looks_like_vpn(text))

    def test_any_of_several_texts_can_match(self):
        self.assertTrue(looks_like_vpn("Ethernet 2", "WireGuard"))


ADAPTERS = [{"name": "Ethernet", "ip": "192.168.1.5", "up": True},
            {"name": "WireGuard", "ip": "10.8.0.2", "up": True},
            {"name": "Mullvad", "ip": "10.9.0.2", "up": False}]


class KillSwitchTests(unittest.TestCase):
    def gate(self, settings: dict):
        with mock.patch.object(torrents, "list_adapters", return_value=ADAPTERS):
            return torrent_gate(settings)

    def test_loopback_is_local_and_a_foreign_address_is_not(self):
        self.assertTrue(ip_is_local("127.0.0.1"))
        self.assertFalse(ip_is_local("203.0.113.77"))  # TEST-NET-3: never assigned to this machine
        self.assertFalse(ip_is_local("not an ip"))

    def test_nothing_leaves_until_a_vpn_adapter_is_chosen(self):
        for settings in ({"require_vpn": True, "adapter": ""}, {}):  # VPN is required by default
            with self.subTest(settings=settings), self.assertRaises(TorrentSafetyError) as ctx:
                self.gate(settings)
            self.assertIn("Choose your VPN adapter", str(ctx.exception))

    def test_a_disconnected_adapter_blocks(self):
        with self.assertRaises(TorrentSafetyError) as ctx:
            self.gate({"adapter": "Mullvad"})
        self.assertIn("isn't connected", str(ctx.exception))

    def test_an_adapter_that_does_not_exist_blocks(self):
        with self.assertRaises(TorrentSafetyError):
            self.gate({"adapter": "Gone"})

    def test_a_connected_adapter_gives_the_address_to_bind_to(self):
        ip, description = self.gate({"adapter": "WireGuard"})
        self.assertEqual(ip, "10.8.0.2")
        self.assertIn("WireGuard", description)

    def test_the_vpn_requirement_can_only_be_dropped_explicitly(self):
        ip, description = self.gate({"require_vpn": False})
        self.assertEqual(ip, "")
        self.assertIn("real IP", description)


class SettingsFileTests(TempDirTest):
    def test_round_trip_ignores_bad_values_and_corrupt_files(self):
        path = self.tmp / "t.json"
        with mock.patch.object(torrents, "TORRENT_FILE", path):
            self.assertEqual(torrents.load_torrent_settings(), torrents.TORRENT_DEFAULTS)  # nothing saved yet
            torrents.save_torrent_settings({**torrents.TORRENT_DEFAULTS, "adapter": "WireGuard"})
            self.assertEqual(torrents.load_torrent_settings()["adapter"], "WireGuard")
            path.write_text('{"adapter": 5, "require_vpn": "yes"}')  # wrong types fall back to the defaults
            loaded = torrents.load_torrent_settings()
            self.assertEqual(loaded["adapter"], torrents.TORRENT_DEFAULTS["adapter"])
            self.assertEqual(loaded["require_vpn"], torrents.TORRENT_DEFAULTS["require_vpn"])
            path.write_text("{ not json")
            self.assertEqual(torrents.load_torrent_settings(), torrents.TORRENT_DEFAULTS)


if __name__ == "__main__":
    unittest.main()
