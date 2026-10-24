# SquashUI

Batch disc-image compressor, extractor and organiser (CHD, RVZ, CSO, PS4/PS5 PKG, PS3, Xbox, ZAR), with an
archive.org / torrent / TorBox downloader and library clean-up tools. PySide6 on Qt's native Windows 11 style.

## Run

    python SquashUI.pyw          # double-click on Windows; installs PySide6 and py7zr on first start
    python -m squashui           # same, from a terminal

## Layout

    squashui/
      constants.py  models.py  system.py  formats.py   shared definitions, disk/process helpers, disc and PKG readers
      batch.py                                          the conversion engine (BatchRunner)
      organize.py                                       multi-disc, leftovers, duplicates, playlist repair, tidy names
      downloads.py  links.py  torrents.py  torbox.py  torrent_runners.py
      dlfiles.py                                        locating / revealing / deleting what a download left on disk
      fingerprint.py                                    finding identical files by size, then checksum (CHD: from its header)
      tools.py                                          auto-install / update of chdman, DolphinTool, extractors ...
      migrate.py  bootstrap.py  app.py  __main__.py
      ui/                                               everything that imports Qt
    tests/                                              unittest suite (pytest runs it too)

Rules the tests enforce: nothing outside `ui/` and `app.py` may import Qt, and modules never import each other in a circle.

## Download list: right-click menu

Right-click files in the Download page's list (or press Delete) for: Open folder, Start, Stop, Tick / Untick, Copy name(s) /
link(s), Delete..., and "Remove N finished from the list".

- **Start / Stop** act on just the selected files while the rest of the run carries on. A stopped file keeps its partial
  download, so Start resumes it. Web files start straight away; files for a torrent run (or a run that just ended) are
  lined up and start when it finishes.
- **Delete...** asks whether to only remove the files from the list, or to delete them from disk as well (partial downloads
  included, permanently). Files still downloading are stopped first.

## Organise: finding matches that aren't spelled identically

- **Multi-disc games** treats free text right after the disc marker as that disc's own label, so
  `Resident Evil 2 (Disc 1)(Leon)` and `(Disc 2)(Claire)` are one game. Region / revision tags (`(USA)`, `(Rev 1)`) still
  keep releases apart. *Loose matching* also pairs discs with merely similar names (same folder, no disc number in common,
  same region tags, matching numerals - so Final Fantasy VII is never paired with VIII); those sets are left unticked.
- **Duplicates** can match by *Same name* (as before), *Same title, ignoring region / version tags*, or *Same contents*
  (size, then a start/end fingerprint, then a full SHA-1; CHDs are compared from their header, so the same disc compressed
  differently still matches). *Also look at ROMs and other game files* adds .gba / .nes / .exe / .zip and similar; those
  name matches are left unticked. Contents matches are ticked: they are proven identical.

## Test

    python -m unittest discover -s tests -t .

Engine tests run against stand-in command-line tools, so no chdman or DolphinTool is needed. The window test builds the real
UI off-screen and is skipped when PySide6 isn't installed.

## Build a Windows .exe

    build_exe.bat              # dist\SquashUI\SquashUI.exe + dist\SquashUI-portable.zip
    build_exe.bat onefile      # dist\SquashUI.exe

The build runs the tests first. `tools\` and the saved settings live next to the `.exe`, so the folder stays portable.
