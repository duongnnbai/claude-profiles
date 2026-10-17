# How it works

## The idea

Claude Desktop is an Electron app. Electron keeps everything a running app knows (cookies, the
signed-in session, local storage, settings) in one **user data folder**. Electron also accepts a
`--user-data-dir=<folder>` flag. Two copies of Claude started with two different folders know nothing
about each other: each has its own login, chats and configuration, and both can run at once.

## Code layout

| File | What it does |
|---|---|
| `claude_profiles/core.py` | Profiles: naming, metadata, create/delete, launch logic. Same on every OS |
| `claude_profiles/gui.py` | The Tkinter window |
| `claude_profiles/icons.py` | Draws icons and UI shapes as PNG/ICO/ICNS in pure Python |
| `claude_profiles/platforms/windows.py` | MSIX package, launching, processes, `.lnk` shortcuts, registry, migration |
| `claude_profiles/platforms/macos.py` | `Claude.app`, processes, `.app` launchers |

## Launching a profile

A profile named `Work` is started with its own data folder:

```
Windows:  %LOCALAPPDATA%\Microsoft\WindowsApps\claude-desktop.exe --user-data-dir=%USERPROFILE%\ClaudeProfiles\Claude-Work
macOS:    /Applications/Claude.app/Contents/MacOS/Claude --user-data-dir=~/Library/Application Support/Claude-Work
```

with this environment variable set for that process only:

```
CLAUDE_CONFIG_DIR=~/.claude-work        (%USERPROFILE%\.claude-work on Windows)
```

`--user-data-dir` only separates Electron's own data. The Claude Code engine that Desktop embeds
(the Code tab) reads its sign-in and settings from `CLAUDE_CONFIG_DIR`, and falls back to the shared
`~/.claude` folder when the variable is not set. Setting it per profile keeps the Code tab on the
right account too. Variables that belong to a running Claude Code session (`CLAUDECODE`,
`CLAUDE_CODE_*`) are removed from the environment before launching.

## Windows: the Store launcher

On Windows, Claude Desktop is distributed as an **MSIX package**, installed under
`C:\Program Files\WindowsApps\Claude_<version>_x64__<publisher-id>`. The package registers an
**app execution alias**, `%LOCALAPPDATA%\Microsoft\WindowsApps\claude-desktop.exe`: the supported
way to start a Store app with your own arguments. It always points at the installed version, so
Claude updates need no special handling, and the process inherits the environment it is started
with (that is how `CLAUDE_CONFIG_DIR` gets through).

The app finds the package with `Get-AppxPackage -Name Claude` (one PowerShell call, cached for 30
seconds and saved to `package.json` so the last answer still works when PowerShell is slow or
fails), accepting only packages whose publisher is Anthropic, and reads the alias name from the
package manifest. Starting a profile does not wait for that call: if the alias exists, it is used
directly. If you turned the alias off in *Settings > Apps > Advanced app settings > App
execution aliases*, it starts the package's `app\Claude.exe` directly instead, which behaves the same.

Your regular Claude (the **Default** card) is started the normal way,
`shell:AppsFolder\<PackageFamilyName>!<AppId>`.

### Windows: the Store app and AppData

Started either way, Claude runs **with package identity**, like the Store-installed app always
does. Windows then redirects whatever the app writes under `%APPDATA%` or `%LOCALAPPDATA%` into
`%LOCALAPPDATA%\Packages\Claude_<publisher-id>\LocalCache\`, which Windows deletes when Claude is
uninstalled. That is why your default profile lives in
`...\Packages\Claude_<publisher-id>\LocalCache\Roaming\Claude`.

A profile folder in `%APPDATA%` would therefore end up somewhere other than where it appears to
be, and disappear with an uninstall. So profiles live in `%USERPROFILE%\ClaudeProfiles\Claude-<Name>`,
which is not redirected. `CLAUDE_CONFIG_DIR` (`%USERPROFILE%\.claude-<name>`) is not redirected
either.

Earlier versions avoided package identity by running a private copy of Claude from
`%LOCALAPPDATA%\ClaudeProfiles\app`, and kept profiles in `%APPDATA%`. On startup this version moves
those profiles to `%USERPROFILE%\ClaudeProfiles` and deletes the copy.

## macOS

`Claude.app` can be run directly with extra arguments, so every profile runs the same installed
app. When Claude updates itself, profiles pick up the new version the next time they start. The
Default card is opened with `open -a Claude.app`.

## Windows: a colored taskbar button per profile

The taskbar groups windows by their **AppUserModelID** (AUMID). Every profile runs the Store
package, so every window carries the package's AUMID and its orange icon. The app gives each
profile its own identity in three places:

1. **The window.** Right after a profile starts, and again every few seconds while Claude Profiles
   is open, the app sets `System.AppUserModel.ID` = `ClaudeProfiles.Profile.<Name>` (plus the
   relaunch command, name and icon) on the profile's windows through
   `SHGetPropertyStoreForWindow`. That splits the profile into its own taskbar button.
2. **A Start Menu shortcut** with the same AUMID,
   `Start Menu\Programs\Claude Profiles\Claude <Name>.lnk`. The taskbar draws the button's icon
   and name from the shortcut that matches the window's AUMID; without one it falls back to
   Claude's own icon. Clicking the shortcut, or the button once pinned, opens that profile.
3. **The window icon** (`WM_SETICON`), which Alt-Tab and the hover preview use.
4. **The tray icon.** Claude draws it itself from the package's `Tray-Win32.ico`, which is
   read-only and shared by every profile, so the file cannot be recolored. Instead the app
   replaces the icon after the fact: Windows identifies a tray icon by its owner window and a
   small numeric id, Electron registers it from a hidden `Electron_NotifyIconHostWindow`, and a
   `Shell_NotifyIcon(NIM_MODIFY)` aimed at that window from another process is accepted. The app
   tries ids 0 to 15 until one is accepted (Electron's numbering is not public), remembers it,
   and sets the profile's colored icon and `Claude <Name>` as the tooltip.

Re-applying on a timer matters because Claude creates a new window when you reopen it from the
Re-applying on a timer matters because Claude creates a new window when you reopen it from the
tray, and redraws its own tray icon on a light/dark mode switch. While Claude Profiles is closed,
a reopened window shows the orange icon until a profile is launched again or the app is opened.
Your default Claude is never touched.

## Detecting running profiles

Every few seconds the app lists Claude processes and reads their command lines:

- **Windows:** `claude.exe` processes, read through the Win32 API (`CreateToolhelp32Snapshot`,
  `NtQueryInformationProcess`) without starting PowerShell.
- **macOS:** `ps -axww -o pid=,args=` (or `psutil`, if it happens to be installed).

Then:

- Helper processes (`--type=renderer`, `--type=gpu-process`, ...) are skipped.
- A main process with `--user-data-dir=<folder>` belongs to the profile that owns that folder.
- A main process of the installed Claude without that flag is your default Claude.
- Claude Code CLI processes (also called `claude`) match neither rule and are ignored.

**Focus** brings the profile's window to the front:

- **Windows:** finds the process's main window and calls `SetForegroundWindow`. If the window is
  hidden in the system tray, the app starts the same command again, and Claude's single-instance
  handling shows the existing window.
- **macOS:** activates the process through `NSRunningApplication` (via `osascript`), which needs no
  Accessibility permission. If the profile's window was closed, click its icon in the Dock.

## Signing in

Claude's sign-in can finish through a `claude://` link opened by your browser. The OS sends that link
to the app registered for the `claude:` protocol, which is your regular Claude, so it may end up in
your default profile instead of the new one.

- **Email sign-in inside the app** usually works without anything special. Close other Claude
  windows first, to be safe.
- **Google / SSO in the browser, Windows:** the experimental menu item writes this key:

  ```
  HKCU\Software\Classes\claude\shell\open\command
    (Default) = "%LOCALAPPDATA%\Microsoft\WindowsApps\claude-desktop.exe" --user-data-dir="<profile folder>" "%1"
  ```

  The link then reaches that profile. **Restore** deletes the key again. The app never
  overwrites a `shell` key that it did not create. Whether Windows prefers this key over the Store
  app's own registration can depend on your Windows version, which is why it is marked experimental.

  Earlier versions registered their private copy of Claude as the `claude:` handler. Once that copy
  is removed, such a key would send every `claude://` link, for the default Claude too, to a program
  that no longer exists. On startup the app deletes a handler that points into its own folder at a
  missing program; handlers that point anywhere else are left alone.
- **Google / SSO in the browser, macOS:** there is no equivalent switch. Quit every other Claude
  (including the default one) before signing in, so the link can only reach the profile you are
  setting up.

## What the app changes on your computer

| Change | When | Undo |
|---|---|---|
| App folder (installed package, icons, `claude-profiles.log`, `package.json` cache) | First launch / `--install` | `--uninstall` |
| `Claude-<Name>` data folder plus a `.claude-profile.json` marker | New profile | *Delete profile...* |
| `~/.claude-<name>` plus a `.claude-profiles-owner` marker | New profile | *Delete profile...* |
| Windows: Desktop / Start menu `.lnk` files | `--install`, *Create Desktop shortcut* | `--uninstall`, delete the file |
| Windows: `Start Menu\Programs\Claude Profiles\Claude <Name>.lnk` (taskbar registration) | First launch of a profile | *Delete profile...*, `--uninstall` |
| macOS: `~/Applications/*.app` launchers (marked with `.claude-profiles-launcher`) | `--install`, *Create app* | `--uninstall`, delete the app |
| Windows: `HKCU\Software\Classes\claude\shell` | Experimental sign-in option only | **Restore**, `--uninstall` |

Nothing system-wide is written (no `HKLM`, no `/Library`), nothing runs at startup, no scheduled
task, service or login item is created, and admin rights are never needed.

## What a profile shares with the others

Profiles share **nothing** on purpose: no MCP config, no skills, no settings. To reuse an MCP server
in a profile, add it again from that profile's Claude settings. That way each profile only ever holds
its own account's tokens and API keys.
