"""Picks the backend for the current operating system."""
from __future__ import annotations

import sys


class UnsupportedPlatform(RuntimeError):
    pass


def get_backend():
    if sys.platform == 'win32':
        from .windows import WindowsBackend
        return WindowsBackend()
    if sys.platform == 'darwin':
        from .macos import MacBackend
        return MacBackend()
    raise UnsupportedPlatform(f'Claude Profiles supports Windows and macOS, not {sys.platform}.')
