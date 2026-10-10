"""What every platform backend provides."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class Proc:
    pid: int
    exe: str
    argv: Optional[list]    # exact arguments when the platform gives them
    cmdline: str            # the whole command line as one string


def flag_value(proc: Proc, flag: str) -> Optional[str]:
    prefix = flag + '='
    if proc.argv is not None:
        for arg in proc.argv[1:]:
            if arg.startswith(prefix):
                return arg[len(prefix):]
        return None
    # No argv split (macOS ps): the value runs until the next " --flag" or the end of the line.
    m = re.search(re.escape(prefix) + r'(?:"([^"]+)"|(.+?))(?=\s--\S|\s*$)', proc.cmdline)
    if not m:
        return None
    return m.group(1) or m.group(2)


def is_helper(proc: Proc) -> bool:
    """Electron child processes (renderer, gpu, ...) carry --type=."""
    if proc.argv is not None:
        return any(a.startswith('--type=') for a in proc.argv[1:])
    return ' --type=' in proc.cmdline


class Backend:
    name = ''
    home: Path
    data_parent: Path          # where the Claude-<Name> profile folders live
    root: Path                 # this app's own folder (installed package, icons, ...)
    not_found_message = 'Claude Desktop was not found. Install it from claude.ai/download.'
    relaunch_shows_window = False
    supports_signin_route = False
    supports_taskbar_branding = False     # a colored taskbar button per profile (Windows)
    shortcut_menu_label = 'Create shortcut'

    @property
    def icon_dir(self) -> Path:
        return self.root / 'icons'

    # The installed Claude
    def claude_version(self) -> Optional[str]: raise NotImplementedError
    def default_data_dir(self) -> Optional[Path]: raise NotImplementedError
    def footer_text(self) -> str: raise NotImplementedError

    def migrate(self) -> list:
        """One-time moves of data left by older versions. Returns messages for the user."""
        return []

    # Processes
    def start_default(self) -> None: raise NotImplementedError
    def start_profile_process(self, profile, env: dict) -> None: raise NotImplementedError
    def running_map(self) -> dict: raise NotImplementedError
    def focus(self, pid: int) -> bool: raise NotImplementedError
    def stop(self, pid: int) -> None: raise NotImplementedError
    def open_folder(self, path: Path) -> None: raise NotImplementedError

    # Shortcuts
    def profile_shortcut_path(self, profile) -> Path: raise NotImplementedError
    def write_profile_shortcut(self, profile, python: Path, args: list) -> Path: raise NotImplementedError
    def install_launchers(self, python: Path, args: list) -> list: raise NotImplementedError
    def remove_launchers(self) -> None: raise NotImplementedError

    def remove_profile_shortcut(self, profile) -> None:
        raise NotImplementedError

    def brand_windows(self, profile, pid: int, python: Path, args: list, wait: float = 0.0) -> int:
        return 0

    # claude:// sign-in routing (Windows only)
    def route_slug(self) -> str:
        return ''

    def set_signin_route(self, profile) -> None:
        raise NotImplementedError

    def clear_signin_route(self) -> None:
        pass
