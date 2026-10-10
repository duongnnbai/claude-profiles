"""Windows shell plumbing through ctypes: .lnk shortcuts and per-window taskbar identity.

The taskbar groups windows by AppUserModelID (AUMID). Every profile runs the Store
package, so all of them carry the package's AUMID and its orange icon. Giving a
profile's windows their own AUMID splits them into their own taskbar button, and a
Start Menu shortcut stamped with the same AUMID tells the taskbar which icon and
name to draw for that button. WM_SETICON covers Alt-Tab and the hover preview.

The tray icon is drawn by Claude itself from the Store package's Tray-Win32.ico, which cannot
be edited per profile. Windows identifies a tray icon by (window, id): Electron registers it
from a hidden Electron_NotifyIconHostWindow, so a Shell_NotifyIcon(NIM_MODIFY) aimed at that
window swaps the icon and tooltip for the profile's own.
"""
from __future__ import annotations

import ctypes
import threading
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Optional

_ole32 = ctypes.WinDLL('ole32')
_shell32 = ctypes.WinDLL('shell32')
_u32 = ctypes.WinDLL('user32')


class GUID(ctypes.Structure):
    _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD), ('Data3', wintypes.WORD),
                ('Data4', ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, text: str) -> 'GUID':
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [('fmtid', GUID), ('pid', wintypes.DWORD)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [('vt', ctypes.c_ushort), ('r1', ctypes.c_ushort), ('r2', ctypes.c_ushort), ('r3', ctypes.c_ushort),
                ('p', ctypes.c_void_p), ('p2', ctypes.c_void_p)]


IID_IPropertyStore = GUID.of('886d8eeb-8cf2-4446-8d02-cdba1dbdcf99')
IID_IShellLinkW = GUID.of('000214F9-0000-0000-C000-000000000046')
IID_IPersistFile = GUID.of('0000010b-0000-0000-C000-000000000046')
CLSID_ShellLink = GUID.of('00021401-0000-0000-C000-000000000046')
_FMTID_AUMID = GUID.of('9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3')
PID_RELAUNCH_COMMAND, PID_RELAUNCH_ICON, PID_RELAUNCH_NAME, PID_APP_ID = 2, 3, 4, 5
VT_EMPTY, VT_LPWSTR = 0, 31

_ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
_ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
_ole32.PropVariantClear.argtypes = [ctypes.POINTER(PROPVARIANT)]
_shell32.SHGetPropertyStoreForWindow.argtypes = [wintypes.HWND, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
_u32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
_u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_u32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
_u32.LoadImageW.restype = wintypes.HANDLE
_u32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM, wintypes.UINT,
                                     wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
_u32.GetSystemMetrics.argtypes = [ctypes.c_int]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD), ('hWnd', wintypes.HWND), ('uID', wintypes.UINT),
                ('uFlags', wintypes.UINT), ('uCallbackMessage', wintypes.UINT), ('hIcon', wintypes.HICON),
                ('szTip', wintypes.WCHAR * 128), ('dwState', wintypes.DWORD), ('dwStateMask', wintypes.DWORD),
                ('szInfo', wintypes.WCHAR * 256), ('uVersion', wintypes.UINT), ('szInfoTitle', wintypes.WCHAR * 64),
                ('dwInfoFlags', wintypes.DWORD), ('guidItem', GUID), ('hBalloonIcon', wintypes.HICON)]


_shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
_shell32.Shell_NotifyIconW.restype = wintypes.BOOL
NIM_MODIFY, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
TRAY_HOST_CLASS = 'Electron_NotifyIconHostWindow'
TRAY_ID_CANDIDATES = range(0, 16)   # Electron numbers its tray icons from a small base; the exact id is not public

_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_local = threading.local()
_icons = {}   # (path, size) -> HICON, kept for the life of the process (windows keep using them)


def _com_init() -> None:
    if not getattr(_local, 'com', False):
        _ole32.CoInitializeEx(None, 0x2)   # COINIT_APARTMENTTHREADED; S_FALSE / RPC_E_CHANGED_MODE are fine
        _local.com = True


def _method(obj: int, index: int, *argtypes):
    """Bind vtable slot ``index`` of a COM interface pointer."""
    vtbl = ctypes.cast(ctypes.c_void_p(obj), ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[index])
    return lambda *args: fn(obj, *args)


def _release(obj: int) -> None:
    if obj:
        _method(obj, 2)()


def _query(obj: int, iid: GUID) -> int:
    out = ctypes.c_void_p()
    hr = _method(obj, 0, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(ctypes.byref(iid), ctypes.byref(out))
    if hr < 0:
        raise OSError(f'QueryInterface failed (0x{hr & 0xFFFFFFFF:08X})')
    return out.value


def _key(pid: int) -> PROPERTYKEY:
    return PROPERTYKEY(_FMTID_AUMID, pid)


def _set_string(store: int, pid: int, value: Optional[str]) -> int:
    pv = PROPVARIANT()
    buf = None
    if value:
        buf = ctypes.create_unicode_buffer(value)   # SetValue copies it
        pv.vt, pv.p = VT_LPWSTR, ctypes.cast(buf, ctypes.c_void_p).value
    key = _key(pid)
    return _method(store, 6, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))(ctypes.byref(key), ctypes.byref(pv))


def _get_string(store: int, pid: int) -> Optional[str]:
    pv = PROPVARIANT()
    key = _key(pid)
    if _method(store, 5, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))(ctypes.byref(key), ctypes.byref(pv)) < 0:
        return None
    try:
        return ctypes.wstring_at(pv.p) if pv.vt == VT_LPWSTR and pv.p else None
    finally:
        _ole32.PropVariantClear(ctypes.byref(pv))


# ---- Shortcuts -----------------------------------------------------------------

def write_shortcut(path: Path, target: Path, args: str, icon: Path, workdir: Path, description: str,
                   app_id: Optional[str] = None) -> None:
    _com_init()
    link = ctypes.c_void_p()
    hr = _ole32.CoCreateInstance(ctypes.byref(CLSID_ShellLink), None, 1, ctypes.byref(IID_IShellLinkW), ctypes.byref(link))
    if hr < 0:
        raise OSError(f'Cannot create a shortcut (0x{hr & 0xFFFFFFFF:08X})')
    link = link.value
    store = persist = 0
    try:
        _method(link, 20, wintypes.LPCWSTR)(str(target))          # SetPath
        _method(link, 11, wintypes.LPCWSTR)(args)                 # SetArguments
        _method(link, 9, wintypes.LPCWSTR)(str(workdir))          # SetWorkingDirectory
        _method(link, 7, wintypes.LPCWSTR)(description)           # SetDescription
        _method(link, 17, wintypes.LPCWSTR, ctypes.c_int)(str(icon), 0)   # SetIconLocation
        if app_id:
            store = _query(link, IID_IPropertyStore)
            _set_string(store, PID_APP_ID, app_id)
            _method(store, 7)()                                   # Commit: shortcuts need it
        persist = _query(link, IID_IPersistFile)
        path.parent.mkdir(parents=True, exist_ok=True)
        hr = _method(persist, 6, wintypes.LPCWSTR, wintypes.BOOL)(str(path), True)   # Save
        if hr < 0:
            raise OSError(f'Cannot save {path} (0x{hr & 0xFFFFFFFF:08X})')
    finally:
        _release(persist)
        _release(store)
        _release(link)


def shortcut_description(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    _com_init()
    link = ctypes.c_void_p()
    if _ole32.CoCreateInstance(ctypes.byref(CLSID_ShellLink), None, 1, ctypes.byref(IID_IShellLinkW),
                               ctypes.byref(link)) < 0:
        return None
    link = link.value
    persist = 0
    try:
        persist = _query(link, IID_IPersistFile)
        if _method(persist, 5, wintypes.LPCWSTR, wintypes.DWORD)(str(path), 0) < 0:   # Load, STGM_READ
            return None
        buf = ctypes.create_unicode_buffer(1024)
        if _method(link, 6, wintypes.LPWSTR, ctypes.c_int)(buf, len(buf)) < 0:      # GetDescription
            return None
        return buf.value
    finally:
        _release(persist)
        _release(link)


# ---- Windows of a running process ----------------------------------------------

def _windows_of(pid: int, class_name: str) -> list:
    found = []

    def visit(hwnd, _lparam):
        owner = wintypes.DWORD()
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            name = ctypes.create_unicode_buffer(64)
            _u32.GetClassNameW(hwnd, name, 64)
            if name.value == class_name:
                found.append(hwnd)
        return True

    _u32.EnumWindows(_WNDENUMPROC(visit), 0)
    return found


def app_windows(pid: int) -> list:
    """Electron's top-level windows (class Chrome_WidgetWin_1) of a process."""
    return _windows_of(pid, 'Chrome_WidgetWin_1')


def tray_host_windows(pid: int) -> list:
    """The hidden window Electron registers its tray icons from."""
    return _windows_of(pid, TRAY_HOST_CLASS)


def has_taskbar_button(hwnd) -> bool:
    """Visible, unowned and not a tool window: the window the taskbar shows a button for."""
    if not _u32.IsWindowVisible(hwnd) or _u32.GetWindow(hwnd, 4):   # GW_OWNER
        return False
    return not (_u32.GetWindowLongW(hwnd, -20) & 0x80)              # GWL_EXSTYLE, WS_EX_TOOLWINDOW


_u32.IsWindowVisible.argtypes = [wintypes.HWND]
_u32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
_u32.GetWindow.restype = wintypes.HWND
_u32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]


def _window_store(hwnd) -> int:
    _com_init()
    store = ctypes.c_void_p()
    if _shell32.SHGetPropertyStoreForWindow(hwnd, ctypes.byref(IID_IPropertyStore), ctypes.byref(store)) < 0:
        return 0
    return store.value or 0


def window_app_id(hwnd) -> Optional[str]:
    store = _window_store(hwnd)
    if not store:
        return None
    try:
        return _get_string(store, PID_APP_ID)
    finally:
        _release(store)


def stamp_window(hwnd, app_id: str, relaunch_command: str, display_name: str, icon: Path) -> bool:
    """Give a window its own taskbar identity. Returns True if it now reports ``app_id``."""
    store = _window_store(hwnd)
    if not store:
        return False
    try:
        _set_string(store, PID_APP_ID, app_id)
        _set_string(store, PID_RELAUNCH_COMMAND, relaunch_command)
        _set_string(store, PID_RELAUNCH_NAME, display_name)
        _set_string(store, PID_RELAUNCH_ICON, f'{icon},0')
        # Commit() fails on window stores and is not needed there.
    finally:
        _release(store)
    return window_app_id(hwnd) == app_id


def _load_icon(ico: Path, size: int):
    key = (str(ico), size)
    icon = _icons.get(key)
    if icon is None:
        icon = _u32.LoadImageW(None, str(ico), 1, size, size, 0x10)   # IMAGE_ICON, LR_LOADFROMFILE
        if icon:
            _icons[key] = icon
    return icon


def set_window_icon(hwnd, ico: Path) -> bool:
    """Replace the icon a window shows in Alt-Tab and the taskbar preview."""
    ok = False
    for which, size in ((1, 32), (0, 16)):   # ICON_BIG, ICON_SMALL
        icon = _load_icon(ico, size)
        if not icon:
            continue
        result = ctypes.c_size_t()
        # SMTO_ABORTIFHUNG: never hang on a busy window.
        if _u32.SendMessageTimeoutW(hwnd, 0x80, which, icon, 0x2, 1000, ctypes.byref(result)):   # WM_SETICON
            ok = True
    return ok


# ---- Tray icon ----------------------------------------------------------------

_tray_ids = {}   # tray host hwnd -> the icon id NIM_MODIFY accepted last time


def _tray_icon_size() -> int:
    """The tray draws 16 px icons scaled to the system DPI; pick the matching ICO frame."""
    try:
        _u32.GetDpiForSystem.restype = wintypes.UINT
        dpi = _u32.GetDpiForSystem()
    except AttributeError:   # before Windows 10 1607
        dpi = 0
    return max(16, round(16 * dpi / 96)) if dpi else (_u32.GetSystemMetrics(49) or 16)   # SM_CXSMICON


def set_tray_icon(host_hwnd, ico: Path, tip: str) -> Optional[int]:
    """Swap the tray icon Electron registered from ``host_hwnd``. Returns the icon id it
    modified, or None if no id was accepted (not an Electron tray window, or no tray icon yet)."""
    icon = _load_icon(ico, _tray_icon_size())
    if not icon:
        return None
    data = NOTIFYICONDATAW()
    data.cbSize = ctypes.sizeof(data)
    data.hWnd = host_hwnd
    data.uFlags = NIF_ICON | NIF_TIP
    data.hIcon = icon
    data.szTip = tip[:127]
    known = _tray_ids.get(host_hwnd)
    candidates = [known] if known is not None else []
    candidates += [i for i in TRAY_ID_CANDIDATES if i != known]
    for uid in candidates:
        data.uID = uid
        if _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data)):
            _tray_ids[host_hwnd] = uid
            return uid
    _tray_ids.pop(host_hwnd, None)
    return None
