"""Profiles: discovery, creation, deletion and launching.

Everything that differs between Windows and macOS lives in ``platforms/``.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

MARKER = '.claude-profile.json'         # marks a profile folder created by this app
OWNER_MARK = '.claude-profiles-owner'   # marks a CLAUDE_CONFIG_DIR folder created by this app
DEFAULT_KEY = '<default>'
PACKAGE = 'claude_profiles'

COLORS = {
    'orange': '#D97757', 'blue': '#3B82F6', 'pink': '#D946A8', 'green': '#16A34A',
    'purple': '#8B5CF6', 'teal': '#0D9488', 'red': '#DC2626', 'yellow': '#CA8A04',
}
COLOR_LABELS = {k: k.capitalize() for k in COLORS}
_HEX_RE = re.compile(r'^#?([0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$')


def normalize_color(text: str) -> str:
    """A preset name ('blue') or a hex color ('#3B82F6', '3b82f6', '#48f') -> the preset name when
    it matches one, else '#RRGGBB'. Raises ValueError for anything else."""
    text = (text or '').strip()
    key = text.lower()
    if key in COLORS:
        return key
    m = _HEX_RE.match(text)
    if not m:
        raise ValueError('Enter a color as #RRGGBB (for example #3B82F6).')
    h = m.group(1).upper()
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    for name, value in COLORS.items():
        if value.upper() == '#' + h:
            return name
    return '#' + h


def color_hex(color: str) -> str:
    """The '#RRGGBB' value of a stored profile color (a preset name or a hex string)."""
    return COLORS.get(color, color)


class ClaudeNotFound(RuntimeError):
    pass


# ---- Log ---------------------------------------------------------------------
# Shortcuts run the app through pythonw.exe, which has no console: when a launch fails, nothing
# would be seen. Every launch and every error is appended to <app folder>/claude-profiles.log.

LOG_NAME = 'claude-profiles.log'
LOG_MAX_BYTES = 256 * 1024
_log_path: Optional[Path] = None


def set_log_dir(folder: Path) -> None:
    global _log_path
    _log_path = folder / LOG_NAME


def log(message: str) -> None:
    """Append one line to the launch log. Never raises."""
    if _log_path is None:
        return
    try:
        _log_path.parent.mkdir(parents=True, exist_ok=True)
        if _log_path.exists() and _log_path.stat().st_size > LOG_MAX_BYTES:
            lines = _log_path.read_text(encoding='utf-8', errors='replace').splitlines()[-500:]
            _log_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        with _log_path.open('a', encoding='utf-8') as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{os.getpid()}] {message}\n")
    except OSError:
        pass


@dataclass
class Profile:
    name: str
    slug: str
    color: str
    data_dir: Optional[Path]      # None for the default profile (the backend knows where it is)
    config_dir: Path
    is_default: bool = False

    @property
    def key(self) -> str:
        """Identifies the profile in the running-process table."""
        return DEFAULT_KEY if self.is_default else norm_path(self.data_dir)


# ---- General helpers ---------------------------------------------------------

def norm_path(path) -> str:
    if not path:
        return ''
    try:
        s = os.path.abspath(str(path))
    except (OSError, ValueError):
        s = str(path)
    return s.rstrip('\\/').lower()


def slugify(name: str) -> str:
    """Strip diacritics (an accented "Cafe" becomes "Cafe", Vietnamese d-stroke becomes "d"),
    then keep only A-Z, 0-9 and "-"."""
    plain = name.strip().replace('đ', 'd').replace('Đ', 'D')
    plain = unicodedata.normalize('NFD', plain)
    plain = ''.join(c for c in plain if unicodedata.category(c) != 'Mn')
    return re.sub(r'[^A-Za-z0-9]+', '-', plain).strip('-')


def safe_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '-', name).strip(' .') or 'Profile'


def rmtree(path: Path) -> None:
    def clear_readonly(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=clear_readonly)
    else:
        shutil.rmtree(path, onerror=clear_readonly)


# ---- Profiles ----------------------------------------------------------------

def _profile_from_dir(backend, data_dir: Path, meta) -> Profile:
    slug = data_dir.name[len('Claude-'):]
    name, color = slug, 'blue'
    if isinstance(meta, dict):
        if meta.get('name'):
            name = str(meta['name'])
        try:
            color = normalize_color(str(meta.get('color') or ''))
        except ValueError:
            pass
    return Profile(name, slug, color, data_dir, backend.home / f'.claude-{slug.lower()}')


def default_profile(backend) -> Profile:
    return Profile('Default', '', 'orange', None, backend.home / '.claude', is_default=True)


def list_profiles(backend) -> list:
    found = []
    try:
        entries = list(backend.data_parent.iterdir())
    except OSError:
        return found
    for d in entries:
        if not d.name.lower().startswith('claude-') or not d.is_dir():
            continue
        marker = d / MARKER
        if not marker.is_file():   # only folders created by this app
            continue
        meta = None
        try:
            meta = json.loads(marker.read_text(encoding='utf-8-sig'))   # the PowerShell version wrote a BOM
        except (OSError, ValueError):
            pass
        found.append(_profile_from_dir(backend, d, meta))
    return sorted(found, key=lambda p: p.name.casefold())


def find_profile(backend, name: str) -> Optional[Profile]:
    wanted = name.casefold()
    for p in list_profiles(backend):
        if p.slug.casefold() == wanted or p.name.casefold() == wanted:
            return p
    if wanted == 'default':
        return default_profile(backend)
    return None


def save_meta(profile: Profile) -> None:
    text = json.dumps({'name': profile.name, 'color': profile.color}, ensure_ascii=False, indent=2)
    (profile.data_dir / MARKER).write_text(text, encoding='utf-8')


def ensure_config_dir(profile: Profile) -> None:
    if not profile.config_dir.exists():
        profile.config_dir.mkdir(parents=True)
        (profile.config_dir / OWNER_MARK).write_text('Created by Claude Profiles', encoding='ascii')


def create_profile(backend, name: str, color: str) -> Profile:
    name = name.strip()
    slug = slugify(name)
    if not slug:
        raise ValueError('The name needs at least one letter or digit (A-Z, 0-9).')
    if len(slug) > 30:
        raise ValueError('The name is too long (30 characters max).')
    color = normalize_color(color)
    data_dir = backend.data_parent / f'Claude-{slug}'
    if data_dir.exists():
        raise ValueError(f'{data_dir} already exists. Pick another name.')
    data_dir.mkdir(parents=True)
    profile = _profile_from_dir(backend, data_dir, {'name': name, 'color': color})
    save_meta(profile)
    ensure_config_dir(profile)
    return profile


def delete_profile(backend, profile: Profile) -> None:
    d = profile.data_dir
    if (profile.is_default or not profile.slug or d is None
            or norm_path(d.parent) != norm_path(backend.data_parent)
            or not d.name.lower().startswith('claude-')
            or not (d / MARKER).is_file()):
        raise RuntimeError('Invalid profile path, nothing was deleted.')
    rmtree(d)
    # Only delete the Claude Code folder if this app created it.
    if (profile.config_dir / OWNER_MARK).is_file():
        rmtree(profile.config_dir)
    backend.remove_profile_shortcut(profile)
    if backend.route_slug() == profile.slug:
        backend.clear_signin_route()


# ---- Launching ---------------------------------------------------------------

def profile_env(profile: Profile) -> dict:
    """The environment for a profile: variables from a running Claude Code session are dropped."""
    env = {}
    for k, v in os.environ.items():
        u = k.upper()
        if u == 'CLAUDECODE' or u.startswith('CLAUDE_CODE_') or u == 'CLAUDE_CONFIG_DIR':
            continue
        env[k] = v
    env['CLAUDE_CONFIG_DIR'] = str(profile.config_dir)
    return env


def start_profile(backend, profile: Profile) -> None:
    ensure_config_dir(profile)
    log(f'start {profile.name}: {backend.describe_launcher()} --user-data-dir={profile.data_dir}')
    backend.start_profile_process(profile, profile_env(profile))
    _brand_when_shown(backend, profile)


def brand_profile(backend, profile: Profile, pid: int, wait: float = 0.0) -> int:
    """Give a running profile's windows its own colored taskbar button (Windows only)."""
    if profile.is_default or not backend.supports_taskbar_branding:
        return 0
    install_package(backend)   # the taskbar button's relaunch shortcut runs the installed copy
    return backend.brand_windows(profile, pid, launcher_python(), launcher_args('--launch', profile.slug), wait)


def _brand_when_shown(backend, profile: Profile, timeout: float = 30.0) -> None:
    """Wait for a just-started profile's window and brand it. Never fails the launch."""
    if profile.is_default or not backend.supports_taskbar_branding:
        return
    deadline = time.monotonic() + timeout
    tray_deadline = None   # the tray icon can appear a little after the window
    try:
        while time.monotonic() < deadline:
            pid = backend.running_map().get(profile.key)
            if pid and brand_profile(backend, profile, pid):
                if backend.tray_branded(pid):
                    return
                if tray_deadline is None:
                    tray_deadline = time.monotonic() + 10
                elif time.monotonic() >= tray_deadline:
                    return
            time.sleep(0.2 if tray_deadline is None else 0.5)
    except Exception:
        pass


def open_profile(backend, profile: Profile) -> str:
    """Open or focus a profile. Returns a status message."""
    pid = backend.running_map().get(profile.key)
    if pid:
        if backend.focus(pid):
            log(f'focus {profile.name}: pid {pid} brought to the front')
            return ''
        log(f'focus {profile.name}: pid {pid} has no visible window, starting again to show it')
        # The window is hidden in the tray: starting it again makes Claude show it.
        if not backend.relaunch_shows_window:
            raise RuntimeError(f"'{profile.name}' is running, but its window could not be brought "
                               "to the front. Click its icon in the Dock.")
        if profile.is_default:
            backend.start_default()
        else:
            start_profile(backend, profile)
        return f'Showing {profile.name}...'
    if profile.is_default:
        log('start Default')
        backend.start_default()
        return 'Opening your default Claude...'
    if not backend.claude_installed():
        raise ClaudeNotFound(backend.not_found_message)
    start_profile(backend, profile)
    return f'Opening {profile.name}...'


# ---- Installing --------------------------------------------------------------

def package_dir() -> Path:
    return Path(__file__).resolve().parent


def _source_hash(folder: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    for f in sorted(folder.rglob('*.py')):
        h.update(f.relative_to(folder).as_posix().encode() + b'\0' + f.read_bytes())
    return h.hexdigest()


_installed = set()   # roots already checked in this run


def install_package(backend, force: bool = False) -> Path:
    """Copy this package next to the app's data, so shortcuts keep working if the download moves.

    Refreshed whenever the running code differs from the installed copy."""
    src, dst = package_dir(), backend.root / PACKAGE
    if norm_path(src) == norm_path(dst) or (not force and str(dst) in _installed):
        return dst
    stamp = backend.root / '.package-hash'
    current = _source_hash(src)
    if dst.exists() and not force:
        try:
            if stamp.read_text(encoding='ascii').strip() == current:
                _installed.add(str(dst))
                return dst
        except OSError:
            pass
    if dst.exists():
        rmtree(dst)
    backend.root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    stamp.write_text(current, encoding='ascii')
    _installed.add(str(dst))
    return dst


def launcher_python() -> Path:
    """The interpreter shortcuts should use (outside any virtualenv, windowless on Windows)."""
    exe = Path(getattr(sys, '_base_executable', '') or sys.executable)
    if sys.platform == 'win32':
        windowless = exe.with_name('pythonw.exe')
        if windowless.exists():
            return windowless
    return exe


def launcher_args(*extra: str) -> list:
    return ['-m', PACKAGE, *extra]


def create_profile_shortcut(backend, profile: Profile) -> Path:
    install_package(backend)
    return backend.write_profile_shortcut(profile, launcher_python(), launcher_args('--launch', profile.slug))
