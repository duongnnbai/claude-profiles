#!/bin/sh
# Removes the launcher apps. Profile data is kept. Double-click in Finder, or run it in Terminal.
cd "$(dirname "$0")" || exit 1
PY=$(command -v python3)
if [ -z "$PY" ]; then
  echo "python3 was not found."
else
  "$PY" -m claude_profiles --uninstall
fi
printf '\nPress Return to close. '; read -r _
