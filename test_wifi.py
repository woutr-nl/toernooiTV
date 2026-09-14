#!/usr/bin/env python3
"""Self-check for the wifi setup page routing, captive redirect and GET /wifi.
Run: python3 test_wifi.py"""

import http.client
import json
import os
import re
import threading
import unittest
from http.server import ThreadingHTTPServer

import server

HTML = os.path.join(server.HERE, "Toernooi TV.dc.html")


def raise_(exc):
    def f():
        raise exc
    return f


class WifiServerTest(unittest.TestCase):
    def setUp(self):
        # serve the real checkout: the page, support.js and vendor/ must be found
        self.cwd = os.getcwd()
        os.chdir(server.HERE)
        self.saved = {k: getattr(server, k) for k in
                      ("device_status", "wifi_radio_state", "comitup_scan", "_comitup")}
        server._scan_cache.update(ts=0.0, nets=None)
        server.device_status = lambda: {"mode": ""}
        server.wifi_radio_state = lambda: "ok"
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        for k, v in self.saved.items():
            setattr(server, k, v)
        os.chdir(self.cwd)

    def get(self, path):
        """GET without following redirects: (status, headers, body)."""
        c = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=10)
        try:
            c.request("GET", path)
            r = c.getresponse()
            return r.status, r.headers, r.read()
        finally:
            c.close()

    def wifi(self):
        status, _, body = self.get("/wifi")
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_setup_routes_serve_app(self):
        for path in ("/setup", "/wifi-setup", "/beheer"):
            status, headers, body = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertIn("text/html", headers["Content-Type"])
            self.assertIn(b"Toernooi TV", body)

    def test_captive_redirect(self):
        server.device_status = lambda: {"mode": "HOTSPOT"}
        status, headers, _ = self.get("/some-random-path")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "http://10.41.0.1/setup")
        self.assertEqual(self.get("/setup")[0], 200)
        self.assertEqual(self.get("/support.js")[0], 200)  # real files pass the captive check

    def test_wifi_radio_and_scan_fields(self):
        server.device_status = lambda: {"mode": "CONNECTED"}
        nets = [{"ssid": "Club", "strength": 70, "secured": True}]
        for radio, scan in (("blocked", ([], "")), ("unavailable", ([], "")),
                            ("ok", ([], "dbus fout")), ("ok", (nets, ""))):
            server.wifi_radio_state = lambda radio=radio: radio
            server.comitup_scan = lambda scan=scan: scan
            w = self.wifi()
            self.assertEqual((w["radio"], w["scanError"], w["networks"]), (radio, scan[1], scan[0]))

    def test_dbus_error_reaches_client(self):
        server._comitup = raise_(Exception("ServiceUnknown"))
        self.assertIn("ServiceUnknown", self.wifi()["scanError"])
        # failed scans aren't cached: the error persists on the next poll
        self.assertIn("ServiceUnknown", self.wifi()["scanError"])

    def test_missing_dbus_module_is_no_error(self):
        server._comitup = raise_(ModuleNotFoundError("No module named 'dbus'"))
        w = self.wifi()
        self.assertEqual((w["scanError"], w["networks"]), ("", []))


class RadioParseTest(unittest.TestCase):
    BLOCKED = "0: phy0: Wireless LAN\n\tSoft blocked: yes\n\tHard blocked: no\n"
    FREE = "0: phy0: Wireless LAN\n\tSoft blocked: no\n\tHard blocked: no\n"

    def test_radio_parsing(self):
        r = server._radio_from
        self.assertEqual(r(self.BLOCKED, "wlan0:wifi:unavailable\n"), "blocked")
        self.assertEqual(r(self.FREE, "eth0:ethernet:connected\nwlan0:wifi:unavailable\n"), "unavailable")
        self.assertEqual(r(self.FREE, "wlan0:wifi:unmanaged\n"), "unavailable")
        self.assertEqual(r(self.FREE, "wlan0:wifi:connected\nlo:loopback:connected (externally)\n"), "ok")
        self.assertEqual(r(self.FREE, "wlan0:wifi:disconnected\n"), "ok")
        self.assertEqual(r("", ""), "")
        self.assertEqual(r(self.FREE, "eth0:ethernet:connected\n"), "")


class ClientRoutingTest(unittest.TestCase):
    def setUp(self):
        with open(HTML, encoding="utf-8") as f:
            self.html = f.read()

    def pattern(self, name):
        m = re.search(r"const %s = /(.+)/;" % name, self.html)
        self.assertIsNotNone(m, name)
        return re.compile(m.group(1))

    def test_setup_and_beheer_routes_split(self):
        setup, beheer = self.pattern("setupRe"), self.pattern("beheerRe")
        for p in ("/setup", "/wifi-setup", "/setup/"):
            self.assertTrue(setup.search(p), p)
            self.assertFalse(beheer.search(p), p)
        for p in ("/beheer", "/instellingen", "/settings"):
            self.assertTrue(beheer.search(p), p)
            self.assertFalse(setup.search(p), p)

    def test_setup_view_wired(self):
        self.assertIn("isDisplay: !isBeheer && !isSetup", self.html)
        self.assertIn('<sc-if value="{{ isSetup }}"', self.html)


if __name__ == "__main__":
    unittest.main()
