"""Command line: `python -m claude_profiles [--launch NAME | --list | --install | --uninstall]`."""
from __future__ import annotations

import argparse
import sys
import traceback

from . import APP_TITLE, core
from .platforms import UnsupportedPlatform, get_backend


def say(text: str = '') -> None:
    if sys.stdout:   # None under pythonw.exe
        print(text)


def error(text: str, dialog: bool = False) -> None:
    core.log(f'error: {text}')
    if sys.stderr:
        print(f'Error: {text}', file=sys.stderr)
    if dialog or not sys.stderr:
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            root.attributes('-topmost', True)   # launched from a shortcut: do not hide behind other windows
            messagebox.showerror(APP_TITLE, text, parent=root)
            root.destroy()
        except Exception:
            pass


def cmd_install(backend) -> int:
    sandbox = backend.sandboxed_by()
    if sandbox:
        raise RuntimeError(
            f'This terminal runs inside the sandbox of the packaged app "{sandbox}" (for example a '
            'terminal opened from Claude Desktop). Files written here are redirected into that '
            "app's private folder and Windows cannot see them, so the shortcuts would not work.\n"
            'Run Install.cmd from File Explorer, or from a terminal opened from the Start menu.')
    target = core.install_package(backend, force=True)
    python = core.launcher_python()
    paths = backend.install_launchers(python, core.launcher_args(backend.root))
    # Refresh profile shortcuts so they point at this version.
    for p in core.list_profiles(backend):
        if backend.profile_shortcut_path(p).exists():
            backend.remove_profile_shortcut(p)
            core.create_profile_shortcut(backend, p)
    say(f"Installed '{APP_TITLE}':")
    for path in paths:
        say(f'  {path}')
    say(f'App files: {target}')
    say(f'Python:    {python}')
    return 0


def cmd_uninstall(backend) -> int:
    backend.clear_signin_route()
    profiles = core.list_profiles(backend)
    for p in profiles:
        backend.remove_profile_shortcut(p)
    backend.remove_launchers()
    if backend.root.exists():
        core.rmtree(backend.root)
    say('The app was removed. Profile data is still here:')
    for p in profiles:
        say(f'  {p.data_dir}   and   {p.config_dir}')
    return 0


def cmd_list(backend) -> int:
    running = backend.running_map()
    for p in [core.default_profile(backend), *core.list_profiles(backend)]:
        state = 'running' if p.key in running else 'stopped'
        say(f'{p.name:<24} {state:<8} {p.data_dir or backend.default_data_dir() or ""}')
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='python -m claude_profiles',
                                 description='Run several Claude Desktop accounts side by side.')
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--launch', metavar='NAME', help='open (or focus) one profile without the window')
    mode.add_argument('--list', action='store_true', help='list profiles and whether they are running')
    mode.add_argument('--install', action='store_true', help='add launchers (Desktop/Start menu or ~/Applications)')
    mode.add_argument('--uninstall', action='store_true', help='remove the app, keep profile data')
    args = ap.parse_args(argv)

    try:
        backend = get_backend()
    except UnsupportedPlatform as e:
        error(str(e))
        return 2
    core.set_log_dir(backend.root)
    core.log(f'run {sys.argv[1:] or ["(window)"]} python={sys.executable}')
    if not args.uninstall:
        try:
            for msg in backend.migrate():
                say(msg)
        except Exception as e:
            error(f'Moving old profile data failed: {e}')

    if args.launch:
        try:
            target = core.find_profile(backend, args.launch)
            if not target:
                raise RuntimeError(f"There is no profile named '{args.launch}'.")
            msg = core.open_profile(backend, target)
            if msg:
                say(msg)
            core.log(f'done {args.launch}: {msg or "focused"}')
            return 0
        except Exception as e:
            core.log(traceback.format_exc().rstrip())
            error(str(e), dialog=True)
            return 1

    try:
        if args.install:
            return cmd_install(backend)
        if args.uninstall:
            return cmd_uninstall(backend)
        if args.list:
            return cmd_list(backend)
    except Exception as e:
        error(str(e))
        return 1

    try:
        from .gui import run
    except ImportError as e:
        error(f'Tkinter is not available ({e}). Install a Python that includes Tk, '
              'e.g. from python.org (macOS: or `brew install python-tk`).', dialog=False)
        return 1
    return run(backend)


if __name__ == '__main__':
    sys.exit(main())
