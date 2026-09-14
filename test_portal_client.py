#!/usr/bin/env python3
"""Self-check for the box side of the portal: sync, config apply, commands and
the managed gating of the local /beheer API. Run: python3 test_portal_client.py"""

import contextlib
import datetime
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import server


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class StubPortal(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        code, resp = self.server.reply(body)
        data = json.dumps(resp).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class PortalClientTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.stub = ThreadingHTTPServer(("127.0.0.1", 0), StubPortal)
        self.stub.requests, self.stub.reply = [], lambda body: (200, {"linked": False})
        threading.Thread(target=self.stub.serve_forever, daemon=True).start()
        self.addCleanup(self.stub.server_close)
        self.addCleanup(self.stub.shutdown)
        p = lambda n: os.path.join(self.tmp, n)  # noqa: E731
        patches = {
            "CONFIG_PATH": p("config.json"), "UPLOADS_DIR": p("uploads"), "UPDATE_STATE_PATH": p("update-state.json"),
            "VERSION": "1.2.3", "BOX_ID": "box-1",
            "PORTAL_URL": "http://127.0.0.1:%d" % self.stub.server_address[1],
            "CONFIG": {"cookie": "SECRETCOOKIEVALUE", "tournaments": [{"code": "t1", "label": "Open", "enabled": True}],
                       "login": {"user": "ts", "pass": "hunter2pass", "name": "TS"}},
            "device_status": lambda: {"mode": "", "online": True, "ssid": "Kantine", "ip": "10.0.0.7", "setupMode": False},
            "list_my_tournaments": lambda: {"ok": True, "tournaments": [{"code": "t1", "name": "Open"}]},
            "_psync": {"results": [], "warnings": [], "myT": None, "myTs": 0.0,
                       "lastOk": 0.0, "lastError": "", "lastErrorAt": 0.0, "errLoggedAt": 0.0},
            "_link": {"code": "AB3-9KF"},
        }
        for k, v in patches.items():
            patcher = mock.patch.object(server, k, v)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def replies(self, *resps):
        """Answer syncs in order; the last reply repeats."""
        queue = list(resps)
        self.stub.reply = lambda body: queue.pop(0) if len(queue) > 1 else queue[0]

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def saved(self):
        with open(server.CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)

    def linked(self, **portal):
        server.CONFIG["portal"] = {**server._clean_portal(None), "secret": "s" * 43, "linked": True, "rev": 1, **portal}

    def test_payload_never_carries_secrets(self):
        server.portal_sync()
        body = self.stub.requests[0]
        for k in ("version", "status", "display", "tournaments", "configRev", "auth", "update", "results"):
            self.assertIn(k, body)
        self.assertEqual((body["boxId"], body["linkCode"], body["configRev"]), ("box-1", "AB3-9KF", 0))
        self.assertEqual(body["display"], server.DISPLAY_DEFAULTS)
        self.assertEqual(body["myTournaments"], [{"code": "t1", "name": "Open"}])
        self.assertGreaterEqual(len(body["secret"]), 40)
        text = json.dumps(body)
        self.assertNotIn("SECRETCOOKIEVALUE", text)
        self.assertNotIn("hunter2pass", text)
        self.assertEqual(self.saved()["portal"]["secret"], body["secret"])  # generated once, persisted
        server.portal_sync()
        self.assertEqual(self.stub.requests[1]["secret"], body["secret"])
        self.assertNotIn("secret", json.dumps(self.call("GET", "/config")[1]))

    def test_applies_portal_config(self):
        self.replies((200, {"linked": True, "name": "Kantine", "configRev": 2, "commands": [], "config": {
            "display": {"clubName": "Kantine", "secCourt": 999, "promoColor": "javascript:x",
                        "clubLogo": "data:image/bmp;base64,Qk0="},
            "tournaments": [{"code": "t2", "label": ""}, {"label": "no code"}]}}))
        server.portal_sync()
        _, cfg = self.call("GET", "/config")
        d = cfg["display"]
        self.assertEqual((d["clubName"], d["secCourt"], d["promoColor"]), ("Kantine", 20, server.DISPLAY_DEFAULTS["promoColor"]))
        self.assertEqual(cfg["tournaments"], [{"code": "t2", "label": "Toernooi", "enabled": True}])
        self.assertEqual((cfg["managed"], cfg["portalName"]), (True, "Kantine"))
        saved = self.saved()
        self.assertEqual((saved["portal"]["rev"], saved["portal"]["linked"], saved["display"]["secCourt"]), (2, True, 20))
        server.portal_sync()
        nxt = self.stub.requests[-1]
        self.assertEqual((nxt["configRev"], nxt["display"]["clubName"]), (2, "Kantine"))
        self.assertNotIn("linkCode", nxt)
        self.assertIn("logo niet opgeslagen", nxt["configWarnings"][0])

    def test_logs_command_with_fake_sudo(self):
        bindir = os.path.join(self.tmp, "bin")
        os.mkdir(bindir)
        with open(os.path.join(bindir, "sudo"), "w") as f:
            f.write('#!/bin/sh\n[ "$1" = "-n" ] && shift\necho "fake journal $*"\n')
        os.chmod(os.path.join(bindir, "sudo"), 0o755)
        self.linked()
        cmd = {"id": 5, "kind": "logs"}
        self.replies((200, {"linked": True, "configRev": 1, "commands": [cmd]}))
        with mock.patch.dict(os.environ, {"PATH": bindir + os.pathsep + os.environ["PATH"]}):
            server.portal_sync()
        self.assertEqual(len(self.stub.requests), 2)  # immediate follow-up carries the result
        (res,) = self.stub.requests[1]["results"]
        self.assertEqual((res["id"], res["ok"]), (5, True))
        self.assertIn("fake journal /usr/bin/journalctl -u toernooitv-server -u toernooitv-kiosk -n 300 --no-pager",
                      res["output"])
        self.assertEqual(self.saved()["portal"]["lastCmdId"], 5)
        server.portal_sync()  # re-delivered (still "sent" in the portal): not run again
        self.assertEqual(self.stub.requests[-1]["results"], [])

    def test_old_commands_skipped(self):
        self.linked(lastCmdId=10)
        self.replies((200, {"linked": True, "configRev": 1, "commands": [{"id": 9, "kind": "restart"}, {"id": 10, "kind": "logs"}]}))
        with mock.patch.object(server.subprocess, "run") as run:
            server.portal_sync()
        run.assert_not_called()

    def test_results_resent_until_delivered(self):
        self.linked()
        self.replies((200, {"linked": True, "configRev": 1, "commands": [{"id": 3, "kind": "clear_cookie"}]}),
                     (500, {}), (200, {"linked": True, "configRev": 1, "commands": []}))
        server.portal_sync()  # runs the command; follow-up fails
        self.assertEqual(server.CONFIG["cookie"], "")
        self.assertEqual(server._psync["results"][0]["output"], "Cookie gewist")
        server.portal_sync()
        self.assertEqual(self.stub.requests[-1]["results"][0]["id"], 3)
        self.assertEqual(server._psync["results"], [])

    def test_restart_reports_after_restart(self):
        self.linked()
        self.replies((200, {"linked": True, "configRev": 1, "commands": [{"id": 7, "kind": "restart"}]}),
                     (200, {"linked": True, "configRev": 1, "commands": []}))
        with mock.patch.object(server.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            server.portal_sync()
        self.assertEqual(run.call_args[0][0], ["sudo", "-n", "/usr/bin/systemctl", "restart", "--no-block",
                                               "toernooitv-server.service", "toernooitv-kiosk.service"])
        self.assertEqual(self.saved()["portal"]["pending"]["id"], 7)
        server.portal_sync()  # same process: not restarted yet → no result
        self.assertEqual(self.stub.requests[-1]["results"], [])
        with mock.patch.object(server, "_STARTED", time.time() + 1):  # "after the restart"
            server.portal_sync()
        self.assertEqual(self.stub.requests[-1]["results"], [{"id": 7, "ok": True, "output": "Herstart voltooid"}])
        self.assertIsNone(self.saved()["portal"]["pending"])

    def test_failed_reboot_reports_failure(self):
        self.linked()
        self.replies((200, {"linked": True, "configRev": 1, "commands": [{"id": 8, "kind": "reboot"}]}),
                     (200, {"linked": True, "configRev": 1, "commands": []}))
        with mock.patch.object(server.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "no")):
            server.portal_sync()
        res = self.stub.requests[-1]["results"][0]
        self.assertEqual((res["id"], res["ok"]), (8, False))
        self.assertIn("install-kiosk.sh", res["output"])
        self.assertIsNone(self.saved()["portal"]["pending"])

    def test_update_resolves_from_state_file(self):
        self.linked()
        self.replies((200, {"linked": True, "configRev": 1, "commands": [{"id": 9, "kind": "update"}]}),
                     (200, {"linked": True, "configRev": 1, "commands": []}))
        with mock.patch.object(server.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            server.portal_sync()
        self.assertEqual(run.call_args[0][0][-1], "toernooitv-update.service")
        with open(server.UPDATE_STATE_PATH, "w") as f:
            json.dump({"status": "running", "startedAt": stamp()}, f)
        server.portal_sync()
        self.assertEqual(self.stub.requests[-1]["results"], [])
        with open(server.UPDATE_STATE_PATH, "w") as f:
            json.dump({"status": "rolled-back", "startedAt": stamp(), "error": "startte niet"}, f)
        server.portal_sync()
        self.assertEqual(self.stub.requests[-1]["results"],
                         [{"id": 9, "ok": False, "output": "rolled-back: startte niet"}])
        # no outcome at all within UPDATE_TIMEOUT
        server.CONFIG["portal"]["pending"] = {"id": 10, "kind": "update", "at": time.time() - server.UPDATE_TIMEOUT - 5}
        os.remove(server.UPDATE_STATE_PATH)
        server.portal_sync()
        self.assertEqual(self.stub.requests[-1]["results"], [{"id": 10, "ok": False, "output": "geen resultaat"}])

    def test_managed_gating(self):
        self.linked()
        s, out = self.call("POST", "/config", {"display": {"clubName": "local"}})
        self.assertEqual((s, out["error"]), (403, server.MANAGED_MSG))
        self.assertEqual(self.call("POST", "/config", {"tournaments": []})[0], 403)
        self.assertEqual(self.call("POST", "/login", {"user": "u", "password": "p"})[0], 403)
        with mock.patch.object(server, "comitup_connect") as conn:
            self.assertEqual(self.call("POST", "/wifi", {"ssid": "Kantine", "password": "x"})[0], 200)
        conn.assert_called_once()
        st = self.call("GET", "/status")[1]
        self.assertTrue(st["managed"])
        self.assertNotIn("linkCode", st)
        server.CONFIG["portal"]["linked"] = False
        st = self.call("GET", "/status")[1]
        self.assertEqual((st["managed"], st["linkCode"]), (False, "AB3-9KF"))
        s, out = self.call("POST", "/config", {"display": {"clubName": "local"}})
        self.assertEqual((s, out["display"]["clubName"]), (200, "local"))

    def test_unlink_keeps_settings(self):
        server.CONFIG["display"] = {**server.DISPLAY_DEFAULTS, "clubName": "Kantine"}
        self.linked(rev=4, lastCmdId=12, name="Kantine")
        self.replies((200, {"linked": False}))
        server.portal_sync()
        p = self.saved()["portal"]
        self.assertEqual((p["linked"], p["rev"], p["lastCmdId"], p["name"]), (False, 0, 0, ""))
        self.assertEqual(self.saved()["display"]["clubName"], "Kantine")
        self.assertNotEqual(server._link["code"], "AB3-9KF")  # fresh code on screen
        self.assertEqual(self.call("POST", "/config", {"display": {"secCourt": 9}})[0], 200)

    def test_no_portal_key_and_url_policy(self):
        self.assertNotIn("portal", server.CONFIG)
        self.assertFalse(self.call("GET", "/config")[1]["managed"])
        self.assertEqual(self.call("POST", "/config", {"display": {"secCourt": 9}})[0], 200)
        self.assertTrue(server._portal_url_ok("https://portal.toernooitv.nl"))
        self.assertTrue(server._portal_url_ok("https://toernooitv.nl"))
        self.assertTrue(server._portal_url_ok("http://localhost:8771"))
        for bad in ("", "http://portal.example.nl", "ftp://x"):
            self.assertFalse(server._portal_url_ok(bad), bad)
        with mock.patch.object(server, "PORTAL_URL", "http://portal.example.nl"):
            st = self.call("GET", "/status")[1]
        self.assertNotIn("linkCode", st)
        self.assertNotIn("portal", st)

    def test_default_portal_url(self):
        env = {k: v for k, v in os.environ.items() if k != "TP_PORTAL_URL"}
        out = subprocess.run([sys.executable, "-c", "import server; print(server.PORTAL_URL)"],
                             env=env, cwd=server.HERE, capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "https://toernooitv.nl")

    def sync_logged(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            server.portal_sync()
        return buf.getvalue()

    def test_portal_unreachable_keeps_local_config(self):
        server.CONFIG["display"] = {**server.DISPLAY_DEFAULTS, "clubName": "Blijft"}
        with mock.patch.object(server, "PORTAL_URL", "http://127.0.0.1:9"):
            self.sync_logged()
        self.assertEqual(self.call("GET", "/config")[1]["display"]["clubName"], "Blijft")

    def test_portal_unreachable_sets_error_and_logs_once(self):
        with mock.patch.object(server, "PORTAL_URL", "http://127.0.0.1:9"):
            log = "".join(self.sync_logged() for _ in range(3))
            self.assertEqual(log.count("mislukt — portaal niet bereikbaar"), 1, log)
            self.assertIn("http://127.0.0.1:9", log)
            p = self.call("GET", "/status")[1]["portal"]
            self.assertEqual(p, {"ok": False, "lastOk": None, "error": "portaal niet bereikbaar"})
            server._psync["errLoggedAt"] -= server.PORTAL_LOG_EVERY + 1  # ~10 minutes later
            log += self.sync_logged()
        self.assertEqual(log.count("mislukt"), 2, log)

    def test_http_500_error(self):
        with mock.patch.object(server, "PORTAL_URL", "http://127.0.0.1:9"):
            log = self.sync_logged()
        self.replies((500, {}))
        log += self.sync_logged()
        p = self.call("GET", "/status")[1]["portal"]
        self.assertEqual((p["ok"], p["error"]), (False, "portaal gaf HTTP 500"))
        self.assertEqual(log.count("mislukt"), 2, log)  # reason changed → logged again

    def test_401_message_kept(self):
        self.replies((401, {}))
        log = self.sync_logged() + self.sync_logged()
        self.assertEqual(log.count("box niet herkend (401) — ontkoppel en koppel hem opnieuw in het portaal"), 1, log)

    def test_error_classification(self):
        import socket, ssl  # noqa: E401
        msg = server._sync_error_msg
        self.assertEqual(msg(urllib.error.URLError(ssl.SSLError(1, "[SSL: TLSV1_ALERT_INTERNAL_ERROR]"))),
                         "certificaat/TLS-fout")
        self.assertEqual(msg(urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))),
                         "portaal niet gevonden (DNS)")
        self.assertEqual(msg(urllib.error.URLError(TimeoutError("timed out"))), "portaal niet bereikbaar")
        self.assertEqual(msg(TimeoutError("timed out")), "portaal niet bereikbaar")
        self.assertEqual(msg(json.JSONDecodeError("x", "", 0)), "ongeldig antwoord")
        self.assertEqual(msg(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")), "ongeldig antwoord")

    def test_recovery_logged_once(self):
        with mock.patch.object(server, "PORTAL_URL", "http://127.0.0.1:9"):
            log = self.sync_logged()
        log += self.sync_logged() + self.sync_logged()
        p = self.call("GET", "/status")[1]["portal"]
        self.assertTrue(p["ok"])
        self.assertIsNone(p["error"])
        self.assertIsInstance(p["lastOk"], float)
        self.assertGreater(p["lastOk"], 0)
        self.assertEqual(log.count("portal: weer bereikbaar"), 1, log)

    def test_link_code_usable_only_after_sync(self):
        st = self.call("GET", "/status")[1]
        self.assertEqual(st["linkCode"], "AB3-9KF")
        self.assertEqual(st["portal"], {"ok": False, "lastOk": None, "error": None})  # "Verbinden met portaal…"
        self.sync_logged()
        self.assertTrue(self.call("GET", "/status")[1]["portal"]["ok"])
        with mock.patch.object(server, "PORTAL_URL", "http://127.0.0.1:9"):
            self.sync_logged()
            st = self.call("GET", "/status")[1]
        self.assertFalse(st["portal"]["ok"])
        self.assertEqual(st["linkCode"], "AB3-9KF")
        server._psync.update(lastError="", lastOk=time.time() - server.PORTAL_STALE - 1)  # stale success
        self.assertFalse(self.call("GET", "/status")[1]["portal"]["ok"])


if __name__ == "__main__":
    unittest.main()
