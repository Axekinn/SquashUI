"""Sidebar groups and per-page titles."""
from __future__ import annotations

from ..organize import ORG_TOOLS


NAV_GROUPS = (
    ("CONVERT", (("chd", "CHD"), ("cso", "CSO"), ("pkg", "PKG  (PS4)"), ("pkg5", "PKG  (PS5)"),
                 ("ps3", "PS3"), ("rvz", "RVZ"), ("xbox", "Xbox"), ("zar", "ZAR"))),
    ("ORGANISE", tuple((t.key, t.nav) for t in sorted(ORG_TOOLS.values(), key=lambda t: t.nav.lower()))),
    ("DOWNLOAD", (("dl", "Files & torrents"),)),
)


PAGE_INFO = {
    "chd": ("CHD conversion", "Convert CD and DVD images (PS1, PS2, Saturn, Dreamcast, CD-i and more) to compressed "
                              "CHD with chdman."),
    "cso": ("CSO conversion", "Convert PSP discs to compressed CSO with maxcso."),
    "pkg": ("PS4 PKG extraction", "Extract PS4 .pkg files into game folders with ShadPS4Plus PKG_Extractor. Can merge "
                                  "each game's updates in and pack the result into a .zar."),
    "pkg5": ("PS5 PKG extraction", "Extract PS5 debug .pkg files with pkg-to-anyps5. Relink them for AnyPS5, or pack "
                                   "the game files into a .zar with eboot.bin at the root for KytyPS5."),
    "ps3": ("PS3 disc decrypting", "Decrypt Redump PS3 ISOs with PS3Dec and .dkey files. Keep the ISO, unpack it to a "
                                   "folder, or both. Output is named Game Name - Title ID from PARAM.SFO."),
    "rvz": ("RVZ conversion", "Convert GameCube and Wii discs to compressed RVZ with DolphinTool."),
    "xbox": ("Xbox XISO conversion", "Repack Xbox and Xbox 360 ISOs as trimmed .xiso files with xdvdfs, verified "
                                     "against the source by checksum. Original Xbox images run in xemu."),
    "zar": ("ZAR packing", "Compress PS4, PS5 and Xbox 360 games and their updates into single .zar files with ZArchive. "
                           "Read directly by shadPS4 (0.17+), KytyPS5 and Xenia Canary."),
    "dl": ("Downloads & torrents", "Download archive.org items, file listings, 1fichier links, magnets and .torrent "
                                   "files, with resume and verification."),
    **{t.key: (t.title, t.blurb) for t in ORG_TOOLS.values()},
}
