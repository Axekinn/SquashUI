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
      tools.py                                          auto-install / update of chdman, DolphinTool, extractors ...
      migrate.py  bootstrap.py  app.py  __main__.py
      ui/                                               everything that imports Qt
    tests/                                              unittest suite (pytest runs it too)

Rules the tests enforce: nothing outside `ui/` and `app.py` may import Qt, and modules never import each other in a circle.

## Test

    python -m unittest discover -s tests -t .

Engine tests run against stand-in command-line tools, so no chdman or DolphinTool is needed. The window test builds the real
UI off-screen and is skipped when PySide6 isn't installed.

## Build a Windows .exe

    build_exe.bat              # dist\SquashUI\SquashUI.exe + dist\SquashUI-portable.zip
    build_exe.bat onefile      # dist\SquashUI.exe

The build runs the tests first. `tools\` and the saved settings live next to the `.exe`, so the folder stays portable.
