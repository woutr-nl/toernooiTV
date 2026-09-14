#!/usr/bin/env python3
"""Self-check for box-stored display settings. Run: python3 test_settings.py"""

import base64
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import server

# 1×1 transparent PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
PNG_URL = "data:image/png;base64," + base64.b64encode(PNG).decode()


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.cwd = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)
        server.CONFIG_PATH = os.path.join(self.tmp, "config.json")
        server.UPLOADS_DIR = os.path.join(self.tmp, "uploads")
        server.CONFIG = {"cookie": "", "tournaments": [], "login": {"user": "", "pass": ""}}
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        server.device_status = lambda: {"mode": ""}  # no nmcli/dbus probing in tests

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        os.chdir(self.cwd)
        shutil.rmtree(self.tmp)

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as r:
            return r.status, r.headers, r.read()

    def post(self, body):
        req = urllib.request.Request(self.base + "/config", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    def cfg(self):
        return json.loads(self.get("/config")[2])

    def test_fresh_box_has_no_display(self):
        self.assertIsNone(self.cfg()["display"])

    def test_round_trip_and_restart(self):
        out = self.post({"display": {"clubName": "X ", "secCourt": 12, "showPromo": False}})
        self.assertTrue(out["ok"])
        d = self.cfg()["display"]
        self.assertEqual((d["clubName"], d["secCourt"], d["showPromo"]), ("X ", 12, False))
        self.assertEqual(d["promoCta"], server.DISPLAY_DEFAULTS["promoCta"])
        with open(server.CONFIG_PATH, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["display"]["clubName"], "X ")
        # partial update keeps stored values
        self.post({"display": {"sponsorName": "S"}})
        self.assertEqual(self.cfg()["display"]["secCourt"], 12)
        server.CONFIG = {}
        server._load_config()
        self.assertEqual(self.cfg()["display"]["secCourt"], 12)
        self.assertEqual(self.cfg()["display"]["sponsorName"], "S")

    def test_logo_lifecycle(self):
        out = self.post({"display": {"clubLogo": PNG_URL}})
        url = out["display"]["clubLogo"]
        self.assertTrue(url.startswith("/uploads/club-logo.png?v="), url)
        self.assertTrue(os.path.isfile(os.path.join(server.UPLOADS_DIR, "club-logo.png")))
        status, headers, body = self.get(url)
        self.assertEqual((status, body), (200, PNG))
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        # echoing the stored path back keeps it; a foreign path does not
        self.assertEqual(self.post({"display": {"clubLogo": url}})["display"]["clubLogo"], url)
        self.assertEqual(self.post({"display": {"clubLogo": "/uploads/sponsor-logo.png"}})["display"]["clubLogo"], url)
        # bad logos: ok, stored value kept, warning set
        big = "data:image/png;base64," + base64.b64encode(b"\0" * (5 * 1024 * 1024 + 10)).decode()
        for bad in (big, "data:text/html;base64,PGgxPg==", "data:image/bmp;base64,Qk0="):
            out = self.post({"display": {"clubLogo": bad, "clubName": "still saved"}})
            self.assertTrue(out["ok"])
            self.assertIn("logo niet opgeslagen", out["warning"])
            self.assertEqual(out["display"]["clubLogo"], url)
            self.assertEqual(out["display"]["clubName"], "still saved")
        out = self.post({"display": {"clubLogo": ""}})
        self.assertEqual(out["display"]["clubLogo"], "")
        self.assertEqual(os.listdir(server.UPLOADS_DIR), [])

    def test_clamping_and_validation(self):
        d = self.post({"display": {"secCourt": 999, "secSponsor": "x", "tennisResults": -5,
                                   "promoColor": "javascript:x", "tennisColor": "#AbCdEf",
                                   "bogus": 1, "clubName": "n" * 400}})["display"]
        self.assertEqual(d["secCourt"], 20)
        self.assertEqual(d["secSponsor"], server.DISPLAY_DEFAULTS["secSponsor"])
        self.assertEqual(d["tennisResults"], 0)
        self.assertEqual(d["promoColor"], server.DISPLAY_DEFAULTS["promoColor"])
        self.assertEqual(d["tennisColor"], "#AbCdEf")
        self.assertNotIn("bogus", d)
        self.assertEqual(len(d["clubName"]), 300)

    def test_secrets_never_returned(self):
        server.CONFIG["cookie"] = "SECRETCOOKIEVALUE1234"
        server.CONFIG["login"] = {"user": "u", "pass": "hunter2pass", "name": "U"}
        server._save_config()
        body = self.get("/config")[2].decode()
        out = json.dumps(self.post({"display": {"clubName": "Y"}}))
        for text in (body, out):
            self.assertNotIn("SECRETCOOKIEVALUE", text)
            self.assertNotIn("hunter2pass", text)
        for path in ("/config.json", "/config.json.tmp", "/config%2Ejson", "/uploads/../config.json"):
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.get(path)
            self.assertEqual(e.exception.code, 404, path)


if __name__ == "__main__":
    unittest.main()
