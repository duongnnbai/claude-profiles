# Troubleshooting & FAQ

## "Python 3.9 or later is needed" / Python not found

Install Python from [python.org](https://www.python.org/downloads/).

- **Windows:** keep **"py launcher"** ticked in the installer. Tkinter is included by default.
- **macOS:** the python.org installer includes Tk 8.6. With Homebrew, run `brew install python-tk`.

## "Claude Profiles needs Tk 8.6 or later" (macOS)

You are running the `python3` that comes with the Xcode Command Line Tools, which ships an old
Tk 8.5. Install Python from python.org or Homebrew (see above), then run `Install.command` again so
the launcher points at the new Python.

## macOS says Install.command "cannot be opened"

Files downloaded as a ZIP are quarantined. Right-click `Install.command` and choose **Open**, or run it
from Terminal:

```bash
sh Install.command
```

## "Claude Desktop was not found"

**Windows:** the app looks for the Microsoft Store (MSIX) build of Claude. Check that it is installed:

```powershell
Get-AppxPackage -Name Claude | Select-Object Name, Version, Publisher
```

**macOS:** the app looks for `Claude.app` in `/Applications` or `~/Applications`.

If it is missing, install Claude Desktop from [claude.ai/download](https://claude.ai/download).

## The sign-in lands in the wrong window

The browser hands the sign-in result back through a `claude://` link, and the OS opens that link in
your **default** Claude.

1. Quit **every** Claude window, including the default one. Check the system tray (Windows) or the
   Dock (macOS) too.
2. Open only the new profile from Claude Profiles and sign in again.
3. **Windows only:** if you sign in with **Google or SSO** and it still lands in the default Claude:
   - In Claude Profiles, open the profile's **...** menu and choose
     **Receive Google/SSO sign-in link (experimental)**.
   - Sign in.
   - Click **Restore** on the yellow bar.

If a token ended up in the wrong profile, nothing is lost. Sign that profile out and back in to its
own account.

## Where did my profiles go? (Windows, after upgrading)

Earlier versions kept profiles in `%APPDATA%\Claude-<Name>`. This version moves them to
`%USERPROFILE%\ClaudeProfiles\Claude-<Name>` the first time it starts, and shows a message saying
which profiles it moved. A profile that was running at that moment is moved the next time you open
Claude Profiles after quitting it.

## Focus does nothing (macOS)

If you closed the profile's window, the app is still running in the Dock. Click its Dock icon to
open a window again.

## Can I use the same email address in two profiles?

Each profile is a separate login, but one email address is one Claude account. To have a personal and
a work account side by side, you need two different email addresses (or one personal account plus a
Team/Enterprise seat on another address).

## Does it work with the Claude Code CLI in a terminal?

The app isolates the **Code tab inside Claude Desktop**. For the terminal CLI, set the same variable
yourself to use a profile's Claude Code data:

```powershell
$env:CLAUDE_CONFIG_DIR = "$HOME\.claude-work"; claude
```

```bash
CLAUDE_CONFIG_DIR=~/.claude-work claude
```

## A profile's taskbar button is orange instead of its color (Windows)

The color is applied when the profile starts, and every few seconds while Claude Profiles is open.
If you reopened a profile from the system tray while Claude Profiles was closed, open Claude
Profiles (or click the profile's shortcut) and the button turns its color again within a few
seconds. After changing a profile's color, quit and relaunch it if the button keeps the old color:
Explorer caches taskbar icons.

On macOS every profile shows Claude's own icon in the Dock; colored Dock icons are not supported.

## How do I back up or move a profile?

Quit the profile, then copy its `Claude-<Name>` folder (and `.claude-<name>` for Claude Code data)
from the locations in the [README](../README.md#where-things-are-stored). Copy them back to the same
place to restore.

## Is this allowed?

The app starts the real, unmodified Claude Desktop with a standard Chromium/Electron flag, signed in
to accounts you own. It does not patch, inject into or intercept Claude. It is still an unofficial
tool. Read Anthropic's terms for your plan, and use it at your own risk.
