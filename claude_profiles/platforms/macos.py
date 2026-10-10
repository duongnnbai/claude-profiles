"""macOS: Claude.app in /Applications.

Unlike the Windows Store build, the app bundle can be run directly with
--user-data-dir, so no private copy is needed.
"""
from __future__ import annotations

import os
import plistlib
import shlex
import signal
import subprocess
from pathlib import Path
from typing import Optional

from .. import APP_TITLE, __version__, core, icons
from .base import Backend, Proc, flag_value, is_helper

LAUNCHER_MARK = '.claude-profiles-launcher'   # marks an .app bundle created by this app


def parse_ps(text: str) -> list:
    """Parse `ps -axww -o pid=,args=` output."""
    procs = []
    for line in text.splitlines():
        pid, _, cmdline = line.strip().partition(' ')
        try:
            procs.append(Proc(int(pid), '', None, cmdline.strip()))
        except ValueError:
            continue
    return procs


def _is_main(proc: Proc, exe_path: str) -> bool:
    if proc.argv is not None:
        return proc.exe == exe_path or (bool(proc.argv) and proc.argv[0] == exe_path)
    return proc.cmdline == exe_path or proc.cmdline.startswith(exe_path + ' ')


def classify(procs: list, exe_path: str) -> dict:
    """normalized data folder -> PID of the main process; '<default>' for the regular Claude."""
    found = {}
    for p in procs:
        # Helpers live in Contents/Frameworks/Claude Helper*.app and carry --type= anyway.
        if not _is_main(p, exe_path) or is_helper(p):
            continue
        data_dir = flag_value(p, '--user-data-dir')
        found[core.norm_path(data_dir) if data_dir else core.DEFAULT_KEY] = p.pid
    return found


def list_processes() -> list:
    try:
        import psutil
    except ImportError:
        out = subprocess.run(['ps', '-axww', '-o', 'pid=,args='], capture_output=True, text=True).stdout
        return parse_ps(out)
    procs = []
    for p in psutil.process_iter(['pid', 'exe', 'cmdline']):
        argv = p.info.get('cmdline')
        if argv:
            procs.append(Proc(p.info['pid'], p.info.get('exe') or '', list(argv), ' '.join(argv)))
    return procs


class MacBackend(Backend):
    name = 'macos'
    not_found_message = 'Claude Desktop was not found in /Applications. Install it from claude.ai/download.'
    relaunch_shows_window = False
    supports_signin_route = False
    shortcut_menu_label = 'Create app in ~/Applications'

    def __init__(self):
        self.home = Path.home()
        self.data_parent = self.home / 'Library' / 'Application Support'
        self.root = self.data_parent / 'ClaudeProfiles'
        self.launcher_dir = self.home / 'Applications'

    # ---- The installed Claude

    def app_info(self) -> Optional[dict]:
        for app in (Path('/Applications/Claude.app'), self.home / 'Applications' / 'Claude.app'):
            try:
                with open(app / 'Contents' / 'Info.plist', 'rb') as f:
                    plist = plistlib.load(f)
            except (OSError, plistlib.InvalidFileException):
                continue
            if 'anthropic' not in str(plist.get('CFBundleIdentifier', '')).lower():
                continue
            exe = app / 'Contents' / 'MacOS' / plist.get('CFBundleExecutable', 'Claude')
            return {'app': app, 'exe': exe, 'version': str(plist.get('CFBundleShortVersionString', '?'))}
        return None

    def claude_version(self) -> Optional[str]:
        info = self.app_info()
        return info['version'] if info else None

    def default_data_dir(self) -> Optional[Path]:
        return self.data_parent / 'Claude'

    def footer_text(self) -> str:
        info = self.app_info()
        if not info:
            return 'Claude Desktop was not found in /Applications.'
        return f"Claude {info['version']} - {info['app']}"

    # ---- Processes

    def _require(self) -> dict:
        info = self.app_info()
        if not info:
            raise core.ClaudeNotFound(self.not_found_message)
        return info

    def start_default(self) -> None:
        subprocess.Popen(['open', '-a', str(self._require()['app'])])

    def start_profile_process(self, profile, env: dict) -> None:
        exe = self._require()['exe']
        subprocess.Popen([str(exe), f'--user-data-dir={profile.data_dir}'], cwd=str(self.home), env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)

    def running_map(self) -> dict:
        info = self.app_info()
        if not info:
            return {}
        return classify(list_processes(), str(info['exe']))

    def focus(self, pid: int) -> bool:
        # NSRunningApplication needs no Accessibility or Automation permission.
        script = ("ObjC.import('AppKit');"
                  f"var a = $.NSRunningApplication.runningApplicationWithProcessIdentifier({int(pid)});"
                  "(a && !a.isNil()) ? (a.unhide, a.activateWithOptions(3) ? 'ok' : 'no') : 'no'")
        try:
            r = subprocess.run(['osascript', '-l', 'JavaScript', '-e', script],
                               capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return r.stdout.strip() == 'ok'

    def stop(self, pid: int) -> None:
        os.kill(pid, signal.SIGTERM)   # Electron quits cleanly on SIGTERM

    def open_folder(self, path: Path) -> None:
        subprocess.Popen(['open', str(path)])

    # ---- Launcher apps (~/Applications/<Name>.app)

    def _write_app(self, path: Path, python: Path, args: list, color: str, bundle_id: str,
                   background: bool) -> Path:
        if path.exists():
            if not (path / 'Contents' / LAUNCHER_MARK).exists():
                raise RuntimeError(f'{path} already exists and was not created by this app.')
            core.rmtree(path)
        contents = path / 'Contents'
        (contents / 'MacOS').mkdir(parents=True)
        (contents / 'Resources').mkdir()
        plist = {
            'CFBundleName': path.stem, 'CFBundleDisplayName': path.stem,
            'CFBundleIdentifier': bundle_id, 'CFBundleExecutable': 'launcher',
            'CFBundleIconFile': 'icon', 'CFBundlePackageType': 'APPL',
            'CFBundleShortVersionString': __version__, 'LSUIElement': background,
            'NSHighResolutionCapable': True,
        }
        with open(contents / 'Info.plist', 'wb') as f:
            plistlib.dump(plist, f)
        command = ' '.join(shlex.quote(a) for a in [str(python), *args])
        launcher = contents / 'MacOS' / 'launcher'
        launcher.write_text(f'#!/bin/sh\ncd {shlex.quote(str(self.root))} || exit 1\nexec {command}\n',
                            encoding='utf-8')
        launcher.chmod(0o755)
        (contents / 'Resources' / 'icon.icns').write_bytes(icons.icon_file(self.icon_dir, color, 'icns').read_bytes())
        (contents / LAUNCHER_MARK).write_text('Created by Claude Profiles', encoding='ascii')
        return path

    def profile_shortcut_path(self, profile) -> Path:
        return self.launcher_dir / f"Claude {core.safe_filename(profile.name).replace(':', '-')}.app"

    def write_profile_shortcut(self, profile, python: Path, args: list) -> Path:
        self.launcher_dir.mkdir(exist_ok=True)
        return self._write_app(self.profile_shortcut_path(profile), python, args, profile.color,
                               f'io.github.claude-profiles.{profile.slug.lower()}', background=True)

    def remove_profile_shortcut(self, profile) -> None:
        self._remove_app(self.profile_shortcut_path(profile))

    def _remove_app(self, path: Path) -> None:
        if (path / 'Contents' / LAUNCHER_MARK).exists():
            core.rmtree(path)

    def launcher_paths(self) -> list:
        return [self.launcher_dir / f'{APP_TITLE}.app']

    def install_launchers(self, python: Path, args: list) -> list:
        self.launcher_dir.mkdir(exist_ok=True)
        return [self._write_app(p, python, args, 'app', 'io.github.claude-profiles', background=False)
                for p in self.launcher_paths()]

    def remove_launchers(self) -> None:
        for path in self.launcher_paths():
            self._remove_app(path)
