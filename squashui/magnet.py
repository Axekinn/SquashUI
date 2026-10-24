"""Opening magnet: links with SquashUI (Windows): registry registration, and hand-over to a running window."""
from __future__ import annotations

import getpass
import re
import sys
from pathlib import Path

from .constants import APP_DIR, APP_NAME

PROG_ID = f"{APP_NAME}.Magnet"
_CLASSES = r"Software\Classes"
_CAPS = rf"Software\{APP_NAME}\Capabilities"
_SERVER = "SquashUI-" + re.sub(r"[^A-Za-z0-9_.-]", "_", getpass.getuser())
_MAX_BYTES = 64 * 1024


def links_from_args(args: list[str]) -> list[str]:
    return [a for a in args if a.lower().startswith("magnet:")]


# --- registry ---------------------------------------------------------------------------------------------------
def _command() -> str:
    """What Windows runs for a magnet link: the packaged exe, or pythonw + SquashUI.pyw for a source checkout."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" "%1"'
    pyw = APP_DIR / "SquashUI.pyw"
    if not pyw.is_file():
        raise OSError("SquashUI.pyw was not found next to the squashui folder")
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pythonw if pythonw.is_file() else sys.executable}" "{pyw}" "%1"'


def _write_progid(winreg, key_path: str, command: str) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "URL:Magnet Link")
        winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
    icon = Path(sys.executable) if getattr(sys, "frozen", False) else APP_DIR / "squashui.ico"
    if icon.is_file():
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path + r"\DefaultIcon") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, f'"{icon}"' + (",0" if icon.suffix == ".exe" else ""))
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path + r"\shell\open\command") as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, command)


def _delete_tree(winreg, path: str) -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            while True:
                try:
                    child = winreg.EnumKey(k, 0)
                except OSError:
                    break
                _delete_tree(winreg, path + "\\" + child)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except OSError:
        pass


def _read(winreg, path: str, name: str = "") -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            return str(winreg.QueryValueEx(k, name)[0])
    except OSError:
        return None


def register() -> None:
    """Per-user (no admin rights): a ProgID, the plain magnet: key, and an entry in Windows' Default apps list."""
    import winreg
    command = _command()
    _write_progid(winreg, rf"{_CLASSES}\{PROG_ID}", command)
    _write_progid(winreg, rf"{_CLASSES}\magnet", command)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _CAPS) as k:
        winreg.SetValueEx(k, "ApplicationName", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(k, "ApplicationDescription", 0, winreg.REG_SZ, "Convert, organise and download game files")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _CAPS + r"\UrlAssociations") as k:
        winreg.SetValueEx(k, "magnet", 0, winreg.REG_SZ, PROG_ID)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\RegisteredApplications") as k:
        winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _CAPS)


def unregister() -> None:
    import winreg
    ours = _read(winreg, rf"{_CLASSES}\{PROG_ID}\shell\open\command")
    if ours is not None and _read(winreg, rf"{_CLASSES}\magnet\shell\open\command") == ours:
        _delete_tree(winreg, rf"{_CLASSES}\magnet")  # only if it is still ours: another client may have taken it
    _delete_tree(winreg, rf"{_CLASSES}\{PROG_ID}")
    _delete_tree(winreg, rf"Software\{APP_NAME}")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\RegisteredApplications", 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, APP_NAME)
    except OSError:
        pass


def is_registered() -> bool:
    if sys.platform != "win32":
        return False
    import winreg
    return _read(winreg, rf"{_CLASSES}\{PROG_ID}\shell\open\command") is not None


def is_default() -> bool:
    """True when Windows will really send magnet links here (it keeps the person's own choice in UserChoice)."""
    if sys.platform != "win32":
        return False
    import winreg
    choice = _read(winreg, r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\magnet\UserChoice",
                   "ProgId")
    if choice is not None:
        return choice == PROG_ID
    ours = _read(winreg, rf"{_CLASSES}\{PROG_ID}\shell\open\command")
    return ours is not None and _read(winreg, rf"{_CLASSES}\magnet\shell\open\command") == ours


def open_default_apps() -> None:
    import os
    os.startfile(f"ms-settings:defaultapps?registeredAppUser={APP_NAME}")  # type: ignore[attr-defined]


# --- focus ------------------------------------------------------------------------------------------------------
def bring_to_front(window) -> None:
    """Restore and focus the window. Windows refuses plain activateWindow() from a background process (the taskbar
    button just flashes), so on Windows the foreground lock is worked around; elsewhere Qt's own calls are used."""
    from PySide6.QtCore import Qt
    if window.isMinimized():
        window.setWindowState(window.windowState() & ~Qt.WindowState.WindowMinimized)
    window.show()
    window.raise_()
    window.activateWindow()
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.BringWindowToTop.argtypes = [wintypes.HWND]
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
        user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        hwnd = wintypes.HWND(int(window.winId()))
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        if user32.SetForegroundWindow(hwnd):
            return
        # Refused: borrow the input queue of whatever has the focus now (the browser that sent the link), which
        # is allowed to hand it over.
        fg = user32.GetForegroundWindow()
        fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        this_thread = kernel32.GetCurrentThreadId()
        attached = bool(fg_thread and fg_thread != this_thread
                        and user32.AttachThreadInput(this_thread, fg_thread, True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(this_thread, fg_thread, False)
    except Exception:
        pass  # focusing is a nicety; the link has been added either way


# --- one running window -----------------------------------------------------------------------------------------
def send_to_running(links: list[str]) -> bool:
    """Hand links to an already open SquashUI. False when there is none (this process then opens the window)."""
    from PySide6.QtNetwork import QLocalSocket
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(0xFFFFFFFF)  # ASFW_ANY: let the open window take focus
        except Exception:
            pass
    sock = QLocalSocket()
    sock.connectToServer(_SERVER)
    if not sock.waitForConnected(400):
        return False
    sock.write("\n".join(links).encode("utf-8"))
    sock.flush()
    sock.waitForBytesWritten(1000)
    sock.disconnectFromServer()
    return True


def serve(window) -> None:
    """Listen for links sent by later launches; window.open_magnets(links) receives them."""
    from PySide6.QtNetwork import QLocalServer
    server = QLocalServer(window)
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)  # this Windows user only
    QLocalServer.removeServer(_SERVER)
    if not server.listen(_SERVER):
        return  # another window already owns the name; this one just runs on its own

    def on_connection() -> None:
        while (sock := server.nextPendingConnection()) is not None:
            sock.waitForReadyRead(500)
            text = bytes(sock.read(_MAX_BYTES)).decode("utf-8", "replace")
            sock.disconnectFromServer()
            if links := links_from_args(text.split()):
                window.open_magnets(links)

    server.newConnection.connect(on_connection)
