"""Platform-independent tests: `python -m unittest discover -s tests`."""
from __future__ import annotations

import json
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_profiles import core, icons                     # noqa: E402
from claude_profiles.platforms import base, macos           # noqa: E402


class FakeBackend(base.Backend):
    def __init__(self, tmp: Path):
        self.home = tmp / 'home'
        self.data_parent = tmp / 'appdata'
        self.root = tmp / 'root'
        self.home.mkdir()
        self.data_parent.mkdir()
        self.removed_shortcuts = []

    def remove_profile_shortcut(self, profile):
        self.removed_shortcuts.append(profile.slug)


class CompileTests(unittest.TestCase):
    def test_every_module_compiles(self):
        import py_compile
        for path in sorted((Path(core.__file__).parent).rglob('*.py')):
            with self.subTest(path=path.name):
                py_compile.compile(str(path), doraise=True)


class SlugTests(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(core.slugify('  Work  '), 'Work')
        self.assertEqual(core.slugify('Café'), 'Cafe')
        self.assertEqual(core.slugify('Đồng Nai'), 'Dong-Nai')
        self.assertEqual(core.slugify('Client A / B'), 'Client-A-B')
        self.assertEqual(core.slugify('!!!'), '')

    def test_safe_filename(self):
        self.assertEqual(core.safe_filename('A/B:C'), 'A-B-C')


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.b = FakeBackend(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_list_delete(self):
        p = core.create_profile(self.b, 'Work', 'green')
        self.assertEqual(p.data_dir, self.b.data_parent / 'Claude-Work')
        self.assertEqual(p.config_dir, self.b.home / '.claude-work')
        self.assertTrue((p.config_dir / core.OWNER_MARK).is_file())
        (self.b.data_parent / 'Claude-Other').mkdir()          # no marker: not ours
        listed = core.list_profiles(self.b)
        self.assertEqual([(x.name, x.color) for x in listed], [('Work', 'green')])
        self.assertEqual(core.find_profile(self.b, 'work').slug, 'Work')
        self.assertTrue(core.find_profile(self.b, 'default').is_default)
        with self.assertRaises(ValueError):
            core.create_profile(self.b, 'Work', 'blue')
        core.delete_profile(self.b, p)
        self.assertFalse(p.data_dir.exists())
        self.assertFalse(p.config_dir.exists())
        self.assertTrue((self.b.data_parent / 'Claude-Other').exists())
        self.assertEqual(self.b.removed_shortcuts, ['Work'])

    def test_keeps_foreign_config_dir(self):
        (self.b.home / '.claude-work').mkdir()
        p = core.create_profile(self.b, 'Work', 'blue')
        core.delete_profile(self.b, p)
        self.assertTrue((self.b.home / '.claude-work').exists())

    def test_reads_powershell_marker_with_bom(self):
        d = self.b.data_parent / 'Claude-Pro'
        d.mkdir()
        (d / core.MARKER).write_bytes(b'\xef\xbb\xbf' + json.dumps({'name': 'Pro Account', 'color': 'teal'}).encode())
        [p] = core.list_profiles(self.b)
        self.assertEqual((p.name, p.color, p.slug), ('Pro Account', 'teal', 'Pro'))

    def test_refuses_to_delete_outside(self):
        outside = Path(self.tmp.name) / 'Claude-X'
        outside.mkdir()
        (outside / core.MARKER).write_text('{}')
        p = core.Profile('X', 'X', 'blue', outside, self.b.home / '.claude-x')
        with self.assertRaises(RuntimeError):
            core.delete_profile(self.b, p)
        self.assertTrue(outside.exists())

    def test_install_package_refreshes_on_change(self):
        dst = core.install_package(self.b)
        self.assertTrue((dst / 'core.py').is_file())
        (dst / 'core.py').write_text('# stale')
        core._installed.clear()
        core.install_package(self.b)                      # same source hash: left alone
        self.assertEqual((dst / 'core.py').read_text(), '# stale')
        (self.b.root / '.package-hash').write_text('old')
        core._installed.clear()
        core.install_package(self.b)                      # hash differs: refreshed
        self.assertNotEqual((dst / 'core.py').read_text(encoding='utf-8'), '# stale')

    def test_profile_env(self):
        import os
        os.environ['CLAUDE_CODE_ENTRYPOINT'] = 'cli'
        os.environ['CLAUDECODE'] = '1'
        try:
            p = core.Profile('W', 'W', 'blue', self.b.data_parent / 'Claude-W', self.b.home / '.claude-w')
            env = core.profile_env(p)
            self.assertEqual(env['CLAUDE_CONFIG_DIR'], str(p.config_dir))
            self.assertFalse(any(k.upper() in ('CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT') for k in env))
        finally:
            del os.environ['CLAUDE_CODE_ENTRYPOINT'], os.environ['CLAUDECODE']


class ProcessTests(unittest.TestCase):
    EXE = '/Applications/Claude.app/Contents/MacOS/Claude'

    def test_macos_ps(self):
        out = '\n'.join([
            f'  101 {self.EXE}',
            f'  102 {self.EXE} --user-data-dir=/Users/a/Library/Application Support/Claude-Work',
            f'  103 {self.EXE} --type=renderer --user-data-dir=/Users/a/Library/Application Support/Claude-Work',
            '  104 /Applications/Claude.app/Contents/Frameworks/Claude Helper.app/Contents/MacOS/Claude Helper',
            '  105 /usr/local/bin/claude --print hi',
        ])
        found = macos.classify(macos.parse_ps(out), self.EXE)
        self.assertEqual(found, {
            core.DEFAULT_KEY: 101,
            core.norm_path('/Users/a/Library/Application Support/Claude-Work'): 102,
        })

    def test_flag_value_argv(self):
        p = base.Proc(1, 'x', ['x', '--user-data-dir=C:\\A B\\Claude-W', 'claude://x'], '')
        self.assertEqual(base.flag_value(p, '--user-data-dir'), 'C:\\A B\\Claude-W')
        self.assertFalse(base.is_helper(p))

    @unittest.skipUnless(sys.platform == 'win32', 'Windows only')
    def test_windows_classify(self):
        from claude_profiles.platforms import windows
        store = r'C:\Program Files\WindowsApps\Claude_1.0.0.0_x64__abc\app\claude.exe'
        procs = [
            base.Proc(1, store, [store], f'"{store}"'),
            base.Proc(2, store, [store, '--type=gpu-process'], ''),
            base.Proc(3, r'C:\x\app\claude.exe', ['claude.exe', r'--user-data-dir=C:\Users\a\Claude-W'], ''),
            base.Proc(4, r'C:\Users\a\.local\bin\claude.exe', ['claude.exe'], ''),
        ]
        self.assertEqual(windows.classify(procs), {core.DEFAULT_KEY: 1, core.norm_path(r'C:\Users\a\Claude-W'): 3})
        self.assertEqual(windows._split_cmdline('"C:\\a b\\c.exe" --user-data-dir="C:\\d e"'),
                         ['C:\\a b\\c.exe', '--user-data-dir=C:\\d e'])
        windows.list_processes()   # must not raise


@unittest.skipUnless(sys.platform == 'win32', 'Windows only')
class WindowsShellTests(unittest.TestCase):
    def test_shortcut_with_app_id(self):
        import subprocess
        from claude_profiles.platforms import win_shell
        with tempfile.TemporaryDirectory() as tmp:
            lnk = Path(tmp) / 'Claude Tést.lnk'
            ico = Path(tmp) / 'i.ico'
            ico.write_bytes(icons.ico_from_pngs({16: icons.draw_icon('teal', 16)}))
            win_shell.write_shortcut(lnk, Path(sys.executable), '-m claude_profiles --launch T', ico, Path(tmp),
                                     'Claude Desktop - T (teal)', 'ClaudeProfiles.Profile.T')
            self.assertEqual(win_shell.shortcut_description(lnk), 'Claude Desktop - T (teal)')
            ps = ("$f = Get-Item -LiteralPath $env:CP_LNK; "
                  "(New-Object -ComObject Shell.Application).NameSpace($f.DirectoryName).ParseName($f.Name)"
                  ".ExtendedProperty('System.AppUserModel.ID')")
            out = subprocess.run(['powershell', '-NoProfile', '-Command', ps], capture_output=True, text=True,
                                 env=dict(__import__('os').environ, CP_LNK=str(lnk))).stdout.strip()
            self.assertEqual(out, 'ClaudeProfiles.Profile.T')
        self.assertIsNone(win_shell.shortcut_description(Path(tmp) / 'missing.lnk'))

    def test_tray_icon_on_unknown_window(self):
        from claude_profiles.platforms import win_shell
        with tempfile.TemporaryDirectory() as tmp:
            ico = Path(tmp) / 'i.ico'
            ico.write_bytes(icons.ico_from_pngs({s: icons.draw_icon('teal', s) for s in (16, 20, 24, 32)}))
            self.assertIsNone(win_shell.set_tray_icon(0x12345, ico, 'Claude T'))   # no such window: no id accepted
            self.assertEqual(win_shell.tray_host_windows(0), [])
        self.assertGreaterEqual(win_shell._tray_icon_size(), 16)


@unittest.skipUnless(sys.platform == 'win32', 'Windows only')
class WindowsMigrationTests(unittest.TestCase):
    def test_moves_profiles_out_of_appdata_and_removes_copy(self):
        import os
        from unittest import mock
        from claude_profiles.platforms import windows
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            env = {'USERPROFILE': str(t / 'home'), 'APPDATA': str(t / 'roaming'), 'LOCALAPPDATA': str(t / 'local')}
            for d in env.values():
                Path(d).mkdir()
            old = t / 'roaming' / 'Claude-Work'
            old.mkdir()
            (old / core.MARKER).write_text(json.dumps({'name': 'Work', 'color': 'green'}))
            (old / 'Cookies').write_text('x')
            (t / 'roaming' / 'Claude-NotOurs').mkdir()
            copy = t / 'local' / 'ClaudeProfiles' / 'app'
            copy.mkdir(parents=True)
            (copy / '.version').write_text('1.0')
            with mock.patch.dict(os.environ, env):
                b = windows.WindowsBackend()
                messages = b.migrate()
                self.assertEqual(b.migrate(), [])   # nothing left to do the second time
            new = t / 'home' / 'ClaudeProfiles' / 'Claude-Work'
            self.assertTrue((new / 'Cookies').is_file())
            self.assertFalse(old.exists())
            self.assertTrue((t / 'roaming' / 'Claude-NotOurs').exists())
            self.assertFalse(copy.exists())
            self.assertEqual(len(messages), 2)
            [p] = core.list_profiles(b)
            self.assertEqual((p.name, p.data_dir), ('Work', new))


class IconTests(unittest.TestCase):
    def test_png(self):
        data = icons.draw_icon('blue', 32)
        self.assertTrue(data.startswith(b'\x89PNG\r\n\x1a\n'))
        w, h = struct.unpack('>II', data[16:24])
        self.assertEqual((w, h), (32, 32))
        idat_len = struct.unpack('>I', data[33:37])[0]
        raw = zlib.decompress(data[41:41 + idat_len])
        self.assertEqual(len(raw), 32 * (1 + 32 * 4))
        row = 1 + 32 * 4
        center = raw[16 * row + 1 + 16 * 4: 16 * row + 1 + 16 * 4 + 4]
        self.assertEqual(center[3], 255)   # opaque in the middle
        self.assertEqual(raw[1 + 3], 0)    # transparent corner

    def test_wrappers(self):
        png = icons.draw_icon('app', 16)
        big = icons.draw_icon('app', 32)
        ico = icons.ico_from_pngs({32: big, 16: png})
        self.assertEqual(struct.unpack('<HHH', ico[:6]), (0, 1, 2))
        w, _h, _c, _r, _planes, _bpp, size, offset = struct.unpack('<BBBBHHII', ico[6:22])
        self.assertEqual((w, ico[offset:offset + size]), (16, png))   # entries sorted by size
        w, _h, _c, _r, _planes, _bpp, size, offset = struct.unpack('<BBBBHHII', ico[22:38])
        self.assertEqual((w, ico[offset:offset + size]), (32, big))
        icns = icons.icns_from_pngs({'ic07': png})
        self.assertEqual(icns[:4], b'icns')
        self.assertEqual(struct.unpack('>I', icns[4:8])[0], len(icns))


if __name__ == '__main__':
    unittest.main()
