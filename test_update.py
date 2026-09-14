#!/usr/bin/env python3
"""Self-check for version, box id, triggered updates and the install templates.
Run: python3 test_update.py"""

import datetime
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from unittest import mock

import server

HERE = os.path.dirname(os.path.abspath(__file__))


def stamp(minutes_ago):
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        p = lambda n: os.path.join(self.tmp, n)  # noqa: E731
        for k, v in {"CONFIG_PATH": p("config.json"), "VERSION_PATH": p("VERSION"),
                     "BOXID_PATH": p(".boxid"), "UPDATE_STATE_PATH": p("update-state.json"),
                     "VERSION": "1.2.3", "BOX_ID": "box-1",
                     "CONFIG": {"cookie": "", "tournaments": []},
                     "device_status": lambda: {"mode": ""}}.items():
            patcher = mock.patch.object(server, k, v)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as r:
            return json.loads(r.read())

    def post_update(self):
        req = urllib.request.Request(self.base + "/update", data=b"", method="POST")
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    def write_state(self, st):
        with open(server.UPDATE_STATE_PATH, "w", encoding="utf-8") as f:
            f.write(st if isinstance(st, str) else json.dumps(st))

    def test_endpoints_report_version_and_box_id(self):
        for path in ("/health", "/status", "/config"):
            out = self.get(path)
            self.assertEqual((out["version"], out["boxId"]), ("1.2.3", "box-1"), path)

    def test_box_id_is_generated_once_and_stable(self):
        first = server._box_id()
        uuid.UUID(first)  # valid uuid
        self.assertEqual(server._box_id(), first)  # "reboot": read back from .boxid
        with open(server.BOXID_PATH, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), first)
        open(server.BOXID_PATH, "w").close()  # empty file → regenerated
        self.assertNotEqual(server._box_id(), "")

    def test_version_file(self):
        self.assertEqual(server._read_version(), "dev")  # missing
        with open(server.VERSION_PATH, "w", encoding="utf-8") as f:
            f.write("2.0.1\n")
        self.assertEqual(server._read_version(), "2.0.1")
        with mock.patch.object(server, "VERSION", "dev"):
            self.assertEqual(self.get("/health")["version"], "dev")

    def test_update_state_in_config(self):
        self.assertIsNone(self.get("/config")["update"])
        sample = {"status": "rolled-back", "from": "1.0.0", "to": "1.1.0", "startedAt": stamp(3),
                  "finishedAt": stamp(2), "error": "versie 1.1.0 startte niet goed (\"x\")"}
        self.write_state(sample)
        self.assertEqual(self.get("/config")["update"], sample)
        self.write_state("{corrupt")
        self.assertIsNone(self.get("/config")["update"])
        self.write_state({"status": "running", "from": "1.0.0", "to": "", "startedAt": stamp(20)})
        u = self.get("/config")["update"]
        self.assertEqual((u["status"], u["error"]), ("failed", "update afgebroken (time-out)"))

    def test_post_update(self):
        with mock.patch.object(server.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 1)) as run:
            out = self.post_update()
        self.assertFalse(out["ok"])
        self.assertIn("niet geïnstalleerd", out["error"])
        self.assertEqual(run.call_args[0][0], ["sudo", "-n", "/usr/bin/systemctl", "start",
                                               "--no-block", "toernooitv-update.service"])
        with mock.patch.object(server.subprocess, "run", side_effect=FileNotFoundError("sudo")):
            self.assertFalse(self.post_update()["ok"])
        with mock.patch.object(server.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0)):
            self.assertTrue(self.post_update()["ok"])

    def test_duplicate_update_refused(self):
        self.write_state({"status": "running", "from": "1.0.0", "to": "", "startedAt": stamp(1)})
        with mock.patch.object(server.subprocess, "run") as run:
            out = self.post_update()
        self.assertEqual((out["ok"], out["error"]), (False, "update loopt al"))
        run.assert_not_called()


class InstallTemplatesTest(unittest.TestCase):
    def test_templates_render_without_leftovers(self):
        templates = glob.glob(os.path.join(HERE, "appliance", "*.service")) + [os.path.join(HERE, "appliance", "sudoers")]
        for path in templates:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            # same substitution install-kiosk.sh does with sed
            out = text.replace("__APP__", "/home/pi/toernooi-tv").replace("__USER__", "pi").replace("__UID__", "1000")
            self.assertIsNone(re.search(r"__[A-Z]+__", out), path)
        for path in templates + [os.path.join(HERE, n) for n in
                                 ("install-kiosk.sh", "verify-appliance.sh", "appliance/kiosk.sh", "appliance/update.sh")]:
            with open(path, encoding="utf-8") as f:
                self.assertNotIn("woutr", f.read(), path)

    def test_shell_scripts_parse(self):
        for shell, name in (("bash", "install-kiosk.sh"), ("bash", "verify-appliance.sh"),
                            ("bash", "appliance/update.sh"), ("sh", "appliance/kiosk.sh")):
            r = subprocess.run([shell, "-n", os.path.join(HERE, name)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, name + ": " + r.stderr)


if __name__ == "__main__":
    unittest.main()
