#!/bin/sh
# Adds "Claude Profiles.app" to ~/Applications. Double-click in Finder, or run it in Terminal.
cd "$(dirname "$0")" || exit 1
PY=$(command -v python3)
if [ -z "$PY" ] || ! "$PY" -c 'import tkinter, sys; sys.exit(tkinter.TkVersion < 8.6 or sys.version_info < (3, 9))' 2>/dev/null; then
  echo "Python 3.9+ with Tk 8.6 is needed."
  echo "Install it from https://www.python.org/downloads/ or run: brew install python-tk"
else
  "$PY" -m claude_profiles --install
fi
printf '\nPress Return to close. '; read -r _
