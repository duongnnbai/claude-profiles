"""Windows: the Microsoft Store (MSIX) build of Claude.

Profiles start through the package's app execution alias
(%LOCALAPPDATA%\\Microsoft\\WindowsApps\\claude-desktop.exe), which always points at
the installed version and passes arguments and environment through.

Started that way Claude runs with package identity, and Windows redirects its writes
under AppData into the package's LocalCache (deleted when Claude is uninstalled).
Profile folders therefore live outside AppData, in %USERPROFILE%\\ClaudeProfiles.
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import signal
import subprocess
import time
import winreg
from ctypes import wintypes
from pathlib import Path
from typing import Optional

from .. import APP_TITLE, core, icons
from . import win_shell
from .base import Backend, Proc, flag_value, is_helper

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
PROTO_SHELL = r'Software\Classes\claude\shell'

_PACKAGE_QUERY = r'''
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$p = Get-AppxPackage -Name 'Claude' -ErrorAction SilentlyContinue |
  Where-Object { $_.Publisher -like '*Anthropic*' } | Sort-Object Version -Descending | Select-Object -First 1
if ($p) {
  $id = 'Claude'; $exe = 'app\Claude.exe'; $alias = ''
  try {
    $app = @((Get-AppxPackageManifest $p).Package.Applications.Application)[0]
    $id = [string]$app.Id; $exe = [string]$app.Executable
    foreach ($e in @($app.Extensions.Extension)) {
      if ($e.AppExecutionAlias) { $alias = [string]@($e.AppExecutionAlias.ExecutionAlias.Alias)[0] }
    }
  } catch {}
  [pscustomobject]@{ version = $p.Version.ToString(); location = $p.InstallLocation;
                     family = $p.PackageFamilyName; appId = $id; exe = $exe; alias = $alias } | ConvertTo-Json -Compress
}
'''

def _powershell(script: str, env: Optional[dict] = None) -> str:
    r = subprocess.run(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
        capture_output=True, env=env, creationflags=CREATE_NO_WINDOW, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode('utf-8', 'replace').strip() or 'PowerShell failed.')
    return r.stdout.decode('utf-8', 'replace').strip()


# ---- Win32 ---------------------------------------------------------------------

_k32 = ctypes.WinDLL('kernel32', use_last_error=True)
_u32 = ctypes.WinDLL('user32', use_last_error=True)
_ntdll = ctypes.WinDLL('ntdll')
_shell32 = ctypes.WinDLL('shell32')


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD), ('th32ProcessID', wintypes.DWORD),
                ('th32DefaultHeapID', ctypes.c_void_p), ('th32ModuleID', wintypes.DWORD),
                ('cntThreads', wintypes.DWORD), ('th32ParentProcessID', wintypes.DWORD),
                ('pcPriClassBase', wintypes.LONG), ('dwFlags', wintypes.DWORD),
                ('szExeFile', wintypes.WCHAR * 260)]


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [('Length', wintypes.USHORT), ('MaximumLength', wintypes.USHORT), ('Buffer', ctypes.c_void_p)]


_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

_k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
_k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                            ctypes.POINTER(wintypes.DWORD)]
_k32.LocalFree.argtypes = [ctypes.c_void_p]
_ntdll.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p,
                                             wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
_ntdll.NtQueryInformationProcess.restype = wintypes.LONG
_shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
_shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
_shell32.SHGetFolderPathW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR]
_u32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
_u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_u32.IsWindowVisible.argtypes = [wintypes.HWND]
_u32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
_u32.GetWindow.restype = wintypes.HWND
_u32.IsIconic.argtypes = [wintypes.HWND]
_u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_u32.SetForegroundWindow.argtypes = [wintypes.HWND]

_INVALID_HANDLE = ctypes.c_void_p(-1).value
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ProcessCommandLineInformation = 60   # Windows 8.1 and later


def _split_cmdline(cmdline: str) -> list:
    argc = ctypes.c_int(0)
    argv = _shell32.CommandLineToArgvW(cmdline, ctypes.byref(argc))
    if not argv:
        return []
    try:
        return [argv[i] for i in range(argc.value)]
    finally:
        _k32.LocalFree(argv)


def _query_process(pid: int):
    h = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None, None
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        exe = buf.value if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else ''
        needed = wintypes.ULONG(0)
        _ntdll.NtQueryInformationProcess(h, _ProcessCommandLineInformation, None, 0, ctypes.byref(needed))
        cmdline = None
        if needed.value:
            raw = ctypes.create_string_buffer(needed.value)
            if _ntdll.NtQueryInformationProcess(h, _ProcessCommandLineInformation, raw, needed,
                                                ctypes.byref(needed)) == 0:
                us = _UNICODE_STRING.from_buffer(raw)
                cmdline = ctypes.wstring_at(us.Buffer, us.Length // 2) if us.Buffer else ''
        return exe, cmdline
    finally:
        _k32.CloseHandle(h)


def list_processes(image_name: str = 'claude.exe') -> list:
    snap = _k32.CreateToolhelp32Snapshot(0x2, 0)   # TH32CS_SNAPPROCESS
    if not snap or snap == _INVALID_HANDLE:
        return []
    procs = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == image_name:
                pid = entry.th32ProcessID
                exe, cmdline = _query_process(pid)
                if cmdline is not None:
                    procs.append(Proc(pid, exe or '', _split_cmdline(cmdline), cmdline))
            ok = _k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snap)
    return procs


def classify(procs: list) -> dict:
    """normalized data folder -> PID of the main process; '<default>' for the regular Claude."""
    found = {}
    for p in procs:
        if is_helper(p):
            continue
        data_dir = flag_value(p, '--user-data-dir')
        exe = p.exe.lower()
        if data_dir:
            found[core.norm_path(data_dir)] = p.pid
        elif '\\windowsapps\\claude_' in exe and exe.endswith('\\app\\claude.exe'):
            found[core.DEFAULT_KEY] = p.pid
        # Anything else named claude.exe (the Claude Code CLI) is ignored.
    return found


def _main_window(pid: int):
    found = []

    def visit(hwnd, _lparam):
        owner = wintypes.DWORD()
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and _u32.IsWindowVisible(hwnd) and not _u32.GetWindow(hwnd, 4):   # GW_OWNER
            found.append(hwnd)
            return False
        return True

    _u32.EnumWindows(_WNDENUMPROC(visit), 0)
    return found[0] if found else None


def _shell_folder(csidl: int) -> Path:
    buf = ctypes.create_unicode_buffer(260)
    _shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
    return Path(buf.value)


def _delete_key_tree(root, path: str) -> None:
    with winreg.OpenKey(root, path, 0, winreg.KEY_ALL_ACCESS) as key:
        while True:
            try:
                child = winreg.EnumKey(key, 0)
            except OSError:
                break
            _delete_key_tree(root, f'{path}\\{child}')
    winreg.DeleteKey(root, path)


def _key_exists(path: str) -> bool:
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, path))
        return True
    except OSError:
        return False


# ---- Backend -------------------------------------------------------------------

class WindowsBackend(Backend):
    name = 'windows'
    not_found_message = 'Claude Desktop was not found. Install it from claude.ai/download.'
    relaunch_shows_window = True
    supports_signin_route = True
    supports_taskbar_branding = True
    shortcut_menu_label = 'Create Desktop shortcut'

    def __init__(self):
        self.home = Path(os.environ['USERPROFILE'])
        self.local = Path(os.environ['LOCALAPPDATA'])
        # Outside AppData, so package virtualization does not move profile data into LocalCache.
        self.data_parent = self.home / 'ClaudeProfiles'
        self.legacy_data_parent = Path(os.environ['APPDATA'])   # used by versions that ran a copy
        self.root = self.local / 'ClaudeProfiles'
        self.route_file = self.root / 'signin-route.txt'
        self._package = None
        self._package_time = 0.0
        self._registered = {}   # slug -> registration description already checked this run
        self._tray_branded = set()   # pids whose tray icon carries their profile color

    # ---- The installed Claude (MSIX)

    def package(self, max_age: float = 30.0) -> Optional[dict]:
        if self._package is None or time.monotonic() - self._package_time > max_age:
            out = _powershell(_PACKAGE_QUERY)
            self._package = json.loads(out) if out else {}
            self._package_time = time.monotonic()
        return self._package or None

    def claude_version(self) -> Optional[str]:
        pkg = self.package()
        return pkg['version'] if pkg else None

    def default_data_dir(self) -> Optional[Path]:
        pkg = self.package()
        if not pkg:
            return None
        return Path(os.environ['LOCALAPPDATA']) / 'Packages' / pkg['family'] / 'LocalCache' / 'Roaming' / 'Claude'

    def launcher_exe(self) -> Path:
        """The execution alias (stable across updates); the package's own exe if the user turned it off."""
        pkg = self.package()
        if not pkg:
            raise core.ClaudeNotFound(self.not_found_message)
        if pkg.get('alias'):
            alias = self.local / 'Microsoft' / 'WindowsApps' / pkg['alias']
            if os.path.lexists(alias):
                return alias
        return Path(pkg['location']) / pkg.get('exe', r'app\Claude.exe')

    def footer_text(self) -> str:
        pkg = self.package()
        if not pkg:
            return 'Claude Desktop (Microsoft Store build) was not found.'
        return f"Claude {pkg['version']} - profiles in {self.data_parent}"

    def migrate(self) -> list:
        """Move profiles out of %APPDATA% (where the copy-based version kept them) and delete the old copy."""
        messages = []
        try:
            old = [d for d in self.legacy_data_parent.iterdir()
                   if d.name.lower().startswith('claude-') and (d / core.MARKER).is_file()]
        except OSError:
            old = []
        running = self.running_map() if old else {}
        for d in old:
            dst = self.data_parent / d.name
            if dst.exists():
                messages.append(f'Not moving {d}: {dst} already exists.')
            elif core.norm_path(d) in running:
                messages.append(f'Quit the {d.name} profile so it can be moved to {self.data_parent}.')
            else:
                try:
                    self.data_parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(d), str(dst))
                    messages.append(f'Moved {d.name} to {self.data_parent}.')
                except OSError as e:
                    messages.append(f'Could not move {d}: {e}')
                    continue
                slug = d.name[len('Claude-'):]
                if self.route_slug() == slug:
                    self._write_route(dst)
        copy = self.root / 'app'
        if (copy / '.version').is_file():
            prefix = core.norm_path(copy) + '\\'
            if not any(p.exe and core.norm_path(p.exe).startswith(prefix) for p in list_processes()):
                core.rmtree(copy)
                messages.append('Removed the old private copy of Claude.')
        return messages

    # ---- Processes

    def start_default(self) -> None:
        pkg = self.package()
        if not pkg:
            raise core.ClaudeNotFound(self.not_found_message)
        subprocess.Popen(['explorer.exe', f"shell:AppsFolder\\{pkg['family']}!{pkg['appId']}"],
                         creationflags=CREATE_NO_WINDOW)

    def start_profile_process(self, profile, env: dict) -> None:
        exe = self.launcher_exe()
        subprocess.Popen([str(exe), f'--user-data-dir={profile.data_dir}'], cwd=str(self.home), env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, close_fds=True)

    def running_map(self) -> dict:
        return classify(list_processes())

    def focus(self, pid: int) -> bool:
        hwnd = _main_window(pid)
        if not hwnd:
            return False
        if _u32.IsIconic(hwnd):
            _u32.ShowWindow(hwnd, 9)   # SW_RESTORE
        return bool(_u32.SetForegroundWindow(hwnd))

    def stop(self, pid: int) -> None:
        os.kill(pid, signal.SIGTERM)   # TerminateProcess on Windows

    def open_folder(self, path: Path) -> None:
        os.startfile(str(path))

    # ---- Shortcuts

    def _write_shortcut(self, path: Path, python: Path, args: list, color: str, desc: str, app_id: str) -> None:
        win_shell.write_shortcut(path, python, subprocess.list2cmdline(args),
                                 icons.icon_file(self.icon_dir, color, 'ico'), self.root, desc, app_id)

    @staticmethod
    def profile_app_id(profile) -> str:
        return f'ClaudeProfiles.Profile.{profile.slug}'

    def profile_shortcut_path(self, profile) -> Path:
        return _shell_folder(0x10) / f'Claude {core.safe_filename(profile.name)}.lnk'   # CSIDL_DESKTOPDIRECTORY

    def write_profile_shortcut(self, profile, python: Path, args: list) -> Path:
        path = self.profile_shortcut_path(profile)
        self._write_shortcut(path, python, args, profile.color, f'Claude Desktop - {profile.name}',
                             self.profile_app_id(profile))
        return path

    def remove_profile_shortcut(self, profile) -> None:
        for path in (self.profile_shortcut_path(profile), self.registration_path(profile)):
            if path.exists():
                path.unlink()
        self._registered.pop(profile.slug, None)

    def launcher_paths(self) -> list:
        return [_shell_folder(0x10) / f'{APP_TITLE}.lnk',    # Desktop
                _shell_folder(0x02) / f'{APP_TITLE}.lnk']    # CSIDL_PROGRAMS: the Start menu

    def install_launchers(self, python: Path, args: list) -> list:
        paths = self.launcher_paths()
        for p in paths:
            # Same AUMID as the window (gui.py), so a pinned Claude Profiles button stays one button.
            self._write_shortcut(p, python, args, 'app', 'Run several Claude Desktop accounts side by side',
                                 'ClaudeProfiles.App')
        return paths

    def remove_launchers(self) -> None:
        for path in self.launcher_paths():
            if path.exists():
                path.unlink()
        try:
            self.registration_dir().rmdir()   # only if empty: profile registrations are removed per profile
        except OSError:
            pass

    # ---- Taskbar identity: a colored button per profile (see win_shell.py)

    def registration_dir(self) -> Path:
        return _shell_folder(0x02) / APP_TITLE

    def registration_path(self, profile) -> Path:
        return self.registration_dir() / f'Claude {core.safe_filename(profile.name)}.lnk'

    def _register(self, profile, python: Path, args: list) -> None:
        """The Start Menu shortcut the taskbar reads the profile button's icon and name from."""
        wanted = f'Claude Desktop - {profile.name} ({profile.color})'
        if self._registered.get(profile.slug) == wanted:
            return
        path = self.registration_path(profile)
        if win_shell.shortcut_description(path) != wanted:
            self._write_shortcut(path, python, args, profile.color, wanted, self.profile_app_id(profile))
        self._registered[profile.slug] = wanted

    def brand_windows(self, profile, pid: int, python: Path, args: list, wait: float = 0.0) -> int:
        """Give a running profile its own taskbar button with its color.

        Stamps every Electron window of the process (the earlier, the less the button jumps),
        waiting up to ``wait`` seconds for one with a taskbar button. Returns how many of those
        were stamped."""
        self._register(profile, python, args)
        app_id = self.profile_app_id(profile)
        ico = icons.icon_file(self.icon_dir, profile.color, 'ico')
        relaunch = subprocess.list2cmdline([str(python), '-c',
                                            f'import sys; sys.path.insert(0, {str(self.root)!r}); '
                                            'from claude_profiles.__main__ import main; '
                                            f'main(["--launch", {profile.slug!r}])'])
        deadline = time.monotonic() + wait
        while True:
            shown = 0
            for hwnd in win_shell.app_windows(pid):
                if win_shell.window_app_id(hwnd) != app_id:
                    win_shell.stamp_window(hwnd, app_id, relaunch, f'Claude {profile.name}', ico)
                if win_shell.has_taskbar_button(hwnd):
                    win_shell.set_window_icon(hwnd, ico)
                    shown += 1
            if shown or time.monotonic() >= deadline:
                self.brand_tray(profile, pid, ico)
                return shown
            time.sleep(0.15)

    def brand_tray(self, profile, pid: int, ico: Path) -> bool:
        """Give the profile's tray icon its color and name. Claude redraws its own icon now and
        then (theme change, reopened window), so this is re-applied with the taskbar branding."""
        done = False
        for hwnd in win_shell.tray_host_windows(pid):
            if win_shell.set_tray_icon(hwnd, ico, f'Claude {profile.name}') is not None:
                done = True
        if done:
            self._tray_branded.add(pid)
        else:
            self._tray_branded.discard(pid)
        return done

    def tray_branded(self, pid: int) -> bool:
        return pid in self._tray_branded

    # ---- claude:// sign-in link routing (experimental)

    def route_slug(self) -> str:
        try:
            return self.route_file.read_text(encoding='ascii').strip()
        except OSError:
            return ''

    def set_signin_route(self, profile) -> None:
        if _key_exists(PROTO_SHELL) and not self.route_slug():
            raise RuntimeError(f'HKCU\\{PROTO_SHELL} already exists and was not created by this app. '
                               'Not overwriting it.')
        self._write_route(profile.data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.route_file.write_text(profile.slug, encoding='ascii')

    def _write_route(self, data_dir: Path) -> None:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, PROTO_SHELL + r'\open\command') as key:
            winreg.SetValueEx(key, '', 0, winreg.REG_SZ, f'"{self.launcher_exe()}" --user-data-dir="{data_dir}" "%1"')

    def clear_signin_route(self) -> None:
        if self.route_slug() and _key_exists(PROTO_SHELL):
            _delete_key_tree(winreg.HKEY_CURRENT_USER, PROTO_SHELL)
        if self.route_file.exists():
            self.route_file.unlink()
