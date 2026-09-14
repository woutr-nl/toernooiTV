#!/usr/bin/env python3
"""Self-check for install.sh: release-tag selection and syntax.
Run: python3 test_install.py"""

import os
import shutil
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


if __name__ == "__main__":
    unittest.main()
