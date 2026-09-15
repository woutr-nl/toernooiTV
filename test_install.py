#!/usr/bin/env python3
"""Self-check for install.sh: release-tag selection and syntax.
Run: python3 test_install.py"""

import os
import shutil
import struct
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "install.sh")


class InstallTest(unittest.TestCase):
    def repo(self, *tags):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        git = lambda *a: subprocess.run(["git", "-C", d, *a], check=True, capture_output=True)  # noqa: E731
        git("init")
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-m", "x")
        for t in tags:
            git("tag", t)
        return d

    def pick(self, repo):
        r = subprocess.run(["bash", SCRIPT, "--print-tag", repo], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_newest_by_version_sort(self):
        self.assertEqual(self.pick(self.repo("v1.2.3", "v1.9.0", "v1.10.0")), "v1.10.0")

    def test_dash_tags_ignored(self):
        self.assertEqual(self.pick(self.repo("v1.0.0", "v2.0.0-rc1", "v2.0.0-test")), "v1.0.0")
        self.assertEqual(self.pick(self.repo("v2.0.0-rc1")), "")

    def test_no_tags_stays_on_main(self):
        self.assertEqual(self.pick(self.repo()), "")

    def test_syntax(self):
        r = subprocess.run(["bash", "-n", SCRIPT], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_kiosk_wifi_country_steps(self):
        with open(os.path.join(HERE, "install-kiosk.sh"), encoding="utf-8") as f:
            s = f.read()
        for needle in ("do_wifi_country NL", "iw reg set NL", "ieee80211_regdom=NL", "rfkill unblock wifi"):
            self.assertIn(needle, s)
        self.assertLess(s.index("rfkill unblock wifi"), s.index("enable comitup"))
        self.assertIn("enable --now toernooitv-hotspot-dhcp.timer", s)

    def test_kiosk_scripts_syntax(self):
        for script in ("install-kiosk.sh", "verify-appliance.sh", "appliance/hotspot-dhcp.sh"):
            r = subprocess.run(["bash", "-n", os.path.join(HERE, script)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, script + ": " + r.stderr)


def read(path, mode="r"):
    with open(os.path.join(HERE, path), mode, **({} if "b" in mode else {"encoding": "utf-8"})) as f:
        return f.read()


class TestKioskCursor(unittest.TestCase):
    def test_kiosk_sh_installs_hidden_cursor_theme(self):
        s = read("appliance/kiosk.sh")
        for needle in ("hidden-cursor-theme", "ln -sfn", "export XCURSOR_PATH=/usr/share/icons:/usr/share/pixmaps"):
            self.assertIn(needle, s)
        self.assertLess(s.index("hidden-cursor-theme"), s.index('while [ "$i" -lt 60 ]'))
        self.assertLess(s.index("export XCURSOR_PATH="), s.index("exec chromium"))

    def test_kiosk_sh_syntax(self):
        r = subprocess.run(["sh", "-n", os.path.join(HERE, "appliance/kiosk.sh")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_hidden_cursor_theme_files(self):
        theme = os.path.join(HERE, "appliance/hidden-cursor-theme")
        self.assertTrue(os.path.isfile(os.path.join(theme, "index.theme")))
        # A transparent "default" would also blank a real mouse drawn via cursor-shape.
        self.assertFalse(os.path.lexists(os.path.join(theme, "cursors/default")))
        b = read("appliance/hidden-cursor-theme/cursors/left_ptr", "rb")
        self.assertEqual(len(b), 68)
        magic, _hdr, _ver, ntoc, ctype, _size, off = struct.unpack_from("<4s6I", b)
        self.assertEqual((magic, ntoc, ctype), (b"Xcur", 1, 0xFFFD0002))
        _chdr, itype, _sub, _v, w, h, _xh, _yh, _delay, pixel = struct.unpack_from("<10I", b, off)
        self.assertEqual((itype, w, h, pixel), (0xFFFD0002, 1, 1, 0))

    def test_installer_links_cursor_theme(self):
        s = read("install-kiosk.sh")
        self.assertIn(".icons", s)
        self.assertIn("hidden-cursor-theme", s)

    def test_display_page_idles_cursor(self):
        s = read("Toernooi TV.dc.html")
        for needle in ("tv-cursor-idle", "cursor:none", "pointermove"):
            self.assertIn(needle, s)


if __name__ == "__main__":
    unittest.main()
