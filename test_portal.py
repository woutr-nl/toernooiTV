#!/usr/bin/env python3
"""Self-check for the hosted portal (portal/portal.py). Run: python3 test_portal.py"""

import base64
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "portal"))
import portal  # noqa: E402

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
PNG_URL = "data:image/png;base64," + base64.b64encode(PNG).decode()
SECRET = "box-secret-" + "s" * 32
TS_PASS, TS_COOKIE = "hunter2tspass", "SECRETCOOKIEVALUE"


class PortalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.saved = {k: getattr(portal, k) for k in ("DB_PATH", "UPLOADS_DIR", "LOGIN_DELAY")}
        portal.DB_PATH = os.path.join(self.tmp, "portal.db")
        portal.UPLOADS_DIR = os.path.join(self.tmp, "uploads")
        portal.LOGIN_DELAY = 0.2
        portal.init_db()
        portal.set_operator("admin", "adminpass1")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), portal.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.seen = []  # every response body, for the secrets sweep
        self.op = self.login("admin", "adminpass1")

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        for k, v in self.saved.items():
            setattr(portal, k, v)

    # -- helpers --
    def raw(self, method, path, body=None, token=None, csrf=True):
        headers = {"Content-Type": "application/json"}
        if csrf:
            headers["X-Portal"] = "1"
        if token:
            headers["Cookie"] = "portal_session=" + token
        data = json.dumps(body).encode() if body is not None else (None if method == "GET" else b"")
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as r:
                status, hdrs, text = r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            status, hdrs, text = e.code, e.headers, e.read()
        self.seen.append(text.decode("utf-8", "replace"))
        return status, hdrs, text

    def req(self, method, path, body=None, token=None, csrf=True):
        status, _, text = self.raw(method, path, body, token, csrf)
        return status, json.loads(text or b"{}")

    def login(self, user, pw):
        status, hdrs, _ = self.raw("POST", "/api/login", {"username": user, "password": pw}, csrf=False)
        self.assertEqual(status, 200)
        return re.search(r"portal_session=([^;]+)", hdrs["Set-Cookie"]).group(1)

    def sync(self, box="box-a", secret=SECRET, **extra):
        body = {"boxId": box, "secret": secret, "linkCode": "AB3-9KF", "version": "1.2.0",
                "status": {"online": True, "ssid": "Kantine", "ip": "10.0.0.7", "setupMode": False},
                "auth": {"state": "ok", "label": "Actief"}, "cookieSet": True, "cookieHint": "…ALUE",
                "login": {"user": "ts", "name": "TS", "stored": True},
                "tournaments": [{"code": "t1", "label": "Open", "enabled": True}],
                "display": {"clubName": "Club A", "clubLogo": "", "secCourt": 8},
                "configRev": 0, "results": []}
        body.update(extra)
        return self.req("POST", "/api/box/sync", body, csrf=False)

    def club_with_user(self, name, username):
        _, c = self.req("POST", "/api/clubs", {"name": name}, self.op)
        s, _ = self.req("POST", "/api/users", {"username": username, "password": "clubpass1", "clubId": c["id"]}, self.op)
        self.assertEqual(s, 200)
        return c["id"], self.login(username, "clubpass1")

    def linked_box(self, club_id, box="box-a", code="AB3-9KF", secret=SECRET):
        self.assertEqual(self.sync(box, secret, linkCode=code)[1], {"linked": False})
        s, out = self.req("POST", "/api/boxes/link", {"code": code.lower(), "clubId": club_id, "name": "Kantine"}, self.op)
        self.assertEqual((s, out.get("boxId")), (200, box), out)

    def db(self, sql, args=()):
        c = sqlite3.connect(portal.DB_PATH)
        try:
            with c:
                return c.execute(sql, args).fetchall()
        finally:
            c.close()

    # -- tests --
    def test_login_required_and_static(self):
        self.assertEqual(self.req("GET", "/api/boxes")[0], 401)
        self.assertEqual(self.req("GET", "/api/boxes", token="bogus")[0], 401)
        status, hdrs, text = self.raw("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<html", text.lower())
        self.assertEqual(self.raw("GET", "/vendor/react.production.min.js")[0], 200)
        for path in ("/portal.db", "/vendor/../portal/portal.py", "/vendor/%2e%2e/server.py",
                     "/uploads/../portal.db", "/api/nope"):
            self.assertEqual(self.raw("GET", path, token=self.op)[0], 404, path)
        self.assertEqual(self.raw("GET", "/uploads/box-a-club.png")[0], 401)
        # CSRF header required for mutations
        self.assertEqual(self.req("POST", "/api/clubs", {"name": "X"}, self.op, csrf=False)[0], 403)

    def test_login_and_password_storage(self):
        t0 = time.time()
        s, out = self.req("POST", "/api/login", {"username": "admin", "password": "wrong"}, csrf=False)
        self.assertEqual(s, 401)
        self.assertGreaterEqual(time.time() - t0, portal.LOGIN_DELAY)
        self.assertEqual(self.req("POST", "/api/login", {"username": "nobody", "password": "x"}, csrf=False)[0], 401)
        self.assertEqual(self.req("GET", "/api/me", token=self.op)[1],
                         {"username": "admin", "role": "operator", "clubId": None, "clubName": None})
        (h, salt), = self.db("SELECT pass_hash, salt FROM users WHERE username='admin'")
        self.assertNotIn("adminpass1", h)
        self.assertEqual((len(h), len(salt)), (128, 32))
        s, hdrs, _ = self.raw("POST", "/api/logout", {}, self.op)
        self.assertIn("HttpOnly", hdrs["Set-Cookie"])
        self.assertEqual(self.req("GET", "/api/me", token=self.op)[0], 401)

    def test_club_users_and_passwords(self):
        club, tok = self.club_with_user("Club A", "clubby")
        other = self.login("clubby", "clubpass1")
        self.assertEqual(self.req("GET", "/api/me", token=tok)[1]["clubName"], "Club A")
        self.assertEqual(self.req("POST", "/api/users", {"username": "x2", "password": "clubpass1"}, self.op)[0], 400)
        self.assertEqual(self.req("POST", "/api/users", {"username": "clubby", "password": "clubpass1",
                                                         "clubId": club}, self.op)[0], 409)
        self.assertEqual(self.req("POST", "/api/password", {"old": "bad", "new": "newpass12"}, tok)[0], 400)
        self.assertEqual(self.req("POST", "/api/password", {"old": "clubpass1", "new": "short"}, tok)[0], 400)
        self.assertEqual(self.req("POST", "/api/password", {"old": "clubpass1", "new": "newpass12"}, tok)[0], 200)
        self.assertEqual(self.req("GET", "/api/me", token=tok)[0], 200)      # current session kept
        self.assertEqual(self.req("GET", "/api/me", token=other)[0], 401)    # other sessions dropped
        tok = self.login("clubby", "newpass12")
        uid = self.req("GET", "/api/users", token=self.op)[1]["users"][1]["id"]
        self.assertEqual(self.req("POST", "/api/users/%d/password" % uid, {"password": "setbyop1"}, self.op)[0], 200)
        self.assertEqual(self.req("GET", "/api/me", token=tok)[0], 401)
        self.assertEqual(self.req("DELETE", "/api/clubs/%d" % club, None, self.op)[0], 409)  # still has a user
        self.assertEqual(self.req("DELETE", "/api/users/%d" % uid, None, self.op)[0], 200)
        self.assertEqual(self.req("DELETE", "/api/clubs/%d" % club, None, self.op)[0], 200)
        self.assertEqual(self.req("GET", "/api/users", token=self.op)[1]["users"][0]["username"], "admin")

    def test_identity_squatting_and_pinning(self):
        self.assertEqual(self.sync()[1], {"linked": False})
        # someone else claiming the same (public) box id must not lock the real box out
        self.assertEqual(self.sync(secret="attacker-" + "x" * 30, linkCode="ZZZ-ZZZ")[1], {"linked": False})
        club, _ = self.club_with_user("Club A", "clubby")
        s, out = self.req("POST", "/api/boxes/link", {"code": "AB39KF", "clubId": club}, self.op)
        self.assertEqual(s, 200, out)
        self.assertEqual(self.sync()[1]["linked"], True)
        self.assertEqual(self.sync(secret="attacker-" + "x" * 30)[0], 401)  # linked: pinned to its secret
        self.assertEqual(self.db("SELECT COUNT(*) FROM pending")[0][0], 0)

    def test_link_codes(self):
        club, _ = self.club_with_user("Club A", "clubby")
        self.sync()
        self.assertEqual(self.req("POST", "/api/boxes/link", {"code": "XXX-XXX", "clubId": club}, self.op)[0], 404)
        self.assertEqual(self.req("POST", "/api/boxes/link", {"code": "AB3-9KF", "clubId": 999}, self.op)[0], 400)
        self.db("UPDATE pending SET last_seen=?", (time.time() - 200,))  # stale code
        self.assertEqual(self.req("POST", "/api/boxes/link", {"code": "AB3-9KF", "clubId": club}, self.op)[0], 404)
        self.sync()
        # a second box (same id copied) reporting the same code → refuse, don't guess
        self.sync(secret="copycat-" + "c" * 30)
        self.assertEqual(self.req("POST", "/api/boxes/link", {"code": "AB3-9KF", "clubId": club}, self.op)[0], 409)
        self.db("DELETE FROM pending WHERE secret_hash<>?", (portal._sha(SECRET),))
        self.assertEqual(self.req("POST", "/api/boxes/link", {"code": "ab3 9kf", "clubId": club, "name": "Kantine"},
                                  self.op)[0], 200)
        d = self.req("GET", "/api/boxes/box-a", token=self.op)[1]
        self.assertEqual((d["name"], d["display"]["clubName"], d["tournaments"][0]["code"], d["rev"]),
                         ("Kantine", "Club A", "t1", 1))
        out = self.sync()[1]
        self.assertEqual((out["linked"], out["configRev"], out["config"]["display"]["clubName"]), (True, 1, "Club A"))
        self.assertEqual(self.sync(configRev=1)[1].get("config"), None)

    def test_display_revisions_and_settle(self):
        club, tok = self.club_with_user("Club A", "clubby")
        self.linked_box(club)
        self.sync(configRev=1)
        s, _ = self.req("POST", "/api/boxes/box-a/display", {"display": {"clubName": "Nieuw", "clubLogo": PNG_URL}}, tok)
        self.assertEqual(s, 200)
        out = self.sync(configRev=1)[1]
        self.assertEqual((out["configRev"], out["config"]["display"]["clubName"]), (2, "Nieuw"))
        self.assertTrue(out["config"]["display"]["clubLogo"].startswith("data:image/png"))
        d = self.req("GET", "/api/boxes/box-a", token=tok)[1]
        self.assertNotIn("data:", json.dumps(d))
        self.assertTrue(d["logos"]["clubLogo"]["set"])
        preview = d["logos"]["clubLogo"]["preview"]
        status, hdrs, body = self.raw("GET", preview, token=tok)
        self.assertEqual((status, body, hdrs["Content-Type"]), (200, PNG, "image/png"))
        self.assertIn("sandbox", hdrs["Content-Security-Policy"])
        self.assertEqual(self.raw("GET", preview)[0], 401)

        # an edit lands while the box still reports the old revision: no settle
        self.req("POST", "/api/boxes/box-a/tournaments", {"tournaments": [{"code": "t9", "label": "Nieuw"}]}, tok)
        self.sync(configRev=2, display={"clubName": "Nieuw", "clubLogo": "/uploads/club-logo.png?v=1"})
        d = self.req("GET", "/api/boxes/box-a", token=tok)[1]
        self.assertEqual((d["rev"], d["tournaments"][0]["code"], d["synced"]), (3, "t9", False))
        # box applied rev 3: portal adopts the box's effective values
        self.sync(configRev=3, display={"clubName": "Nieuw", "clubLogo": "/uploads/club-logo.png?v=1"},
                  tournaments=[{"code": "t9", "label": "Nieuw", "enabled": True}])
        d = self.req("GET", "/api/boxes/box-a", token=tok)[1]
        self.assertTrue(d["synced"])
        self.assertEqual(d["logos"]["clubLogo"], {"set": True, "preview": preview})
        (desired,), = self.db("SELECT desired FROM boxes")
        self.assertEqual(json.loads(desired)["display"]["clubLogo"], "/uploads/club-logo.png?v=1")
        # the box rejected the logo → preview dropped
        self.sync(configRev=3, display={"clubName": "Nieuw", "clubLogo": ""},
                  configWarnings=["logo niet opgeslagen: logo te groot (max 5 MB)"])
        self.assertEqual(self.req("GET", "/api/boxes/box-a", token=tok)[1]["logos"]["clubLogo"],
                         {"set": False, "preview": None})
        self.assertEqual(os.listdir(portal.UPLOADS_DIR), [])
        # size caps
        self.assertEqual(self.req("POST", "/api/boxes/box-a/display",
                                  {"display": {"clubLogo": "data:image/png;base64," + "A" * 7000004}}, tok)[0], 413)
        self.assertEqual(self.req("POST", "/api/boxes/box-a/display", {"display": {"clubLogo": "data:text/html;base64,PGgxPg=="}}, tok)[0], 400)

    def test_role_enforcement(self):
        club_a, tok_a = self.club_with_user("Club A", "usera")
        club_b, tok_b = self.club_with_user("Club B", "userb")
        self.linked_box(club_a, "box-a", "AAA-AAA")
        self.linked_box(club_b, "box-b", "BBB-BBB", secret="second-" + "b" * 30)
        self.assertEqual([b["boxId"] for b in self.req("GET", "/api/boxes", token=tok_a)[1]["boxes"]], ["box-a"])
        self.assertEqual(len(self.req("GET", "/api/boxes", token=self.op)[1]["boxes"]), 2)
        # another club's box: invisible
        for method, path, body in (("GET", "/api/boxes/box-b", None),
                                   ("POST", "/api/boxes/box-b/display", {"display": {"clubName": "hack"}}),
                                   ("POST", "/api/boxes/box-b/tournaments", {"tournaments": []}),
                                   ("POST", "/api/boxes/box-b/login", {"user": "u", "password": "p"}),
                                   ("POST", "/api/boxes/box-b/cookie", {"clear": True})):
            self.assertEqual(self.req(method, path, body, tok_a)[0], 404, path)
        # operator-only actions, even on their own box
        for method, path, body in (("POST", "/api/boxes/box-a/command", {"kind": "restart"}),
                                   ("POST", "/api/boxes/box-a/command", {"kind": "logs"}),
                                   ("GET", "/api/boxes/box-a/logs", None),
                                   ("POST", "/api/boxes/box-a/unlink", {}),
                                   ("POST", "/api/boxes/box-a", {"clubId": club_b}),
                                   ("POST", "/api/boxes/link", {"code": "AAA-AAA", "clubId": club_a}),
                                   ("GET", "/api/clubs", None), ("GET", "/api/users", None),
                                   ("POST", "/api/users", {"username": "evil", "password": "evilpass1", "clubId": club_a})):
            self.assertEqual(self.req(method, path, body, tok_a)[0], 403, path)
        self.assertEqual(self.db("SELECT COUNT(*) FROM commands")[0][0], 0)
        self.assertEqual(self.req("POST", "/api/boxes/box-a/display", {"display": {"clubName": "ok"}}, tok_a)[0], 200)

        # logs: requested by the operator, never visible to the club user
        _, cmd = self.req("POST", "/api/boxes/box-a/command", {"kind": "logs"}, self.op)
        self.req("POST", "/api/boxes/box-a/command", {"kind": "restart"}, self.op)
        self.req("POST", "/api/boxes/box-a/login", {"user": "ts", "password": TS_PASS}, tok_a)
        cmds = self.sync("box-a", linkCode="AAA-AAA")[1]["commands"]
        self.assertEqual([c["kind"] for c in cmds], ["logs", "restart", "set_login"])
        self.sync("box-a", results=[{"id": cmd["commandId"], "ok": True, "output": "JOURNAL-LINE-42"}])
        hist = self.req("GET", "/api/boxes/box-a", token=tok_a)[1]["commands"]
        self.assertEqual([c["kind"] for c in hist], ["set_login"])
        op_detail = self.req("GET", "/api/boxes/box-a", token=self.op)[1]
        self.assertEqual({c["kind"] for c in op_detail["commands"]}, {"logs", "restart", "set_login"})
        self.assertNotIn("JOURNAL-LINE-42", json.dumps(op_detail))
        self.assertNotIn("JOURNAL-LINE-42", json.dumps(self.req("GET", "/api/boxes/box-a", token=tok_a)[1]))
        self.assertEqual(self.req("GET", "/api/boxes/box-a/logs", token=self.op)[1]["logs"], "JOURNAL-LINE-42")

    def test_reassign_box(self):
        club_a, tok_a = self.club_with_user("Club A", "usera")
        club_b, tok_b = self.club_with_user("Club B", "userb")
        self.linked_box(club_a)
        self.assertEqual(self.req("POST", "/api/boxes/box-a", {"clubId": 999}, self.op)[0], 400)
        self.assertEqual(self.req("POST", "/api/boxes/box-a", {"clubId": club_b, "name": "Bar"}, self.op)[0], 200)
        self.assertEqual(self.req("GET", "/api/boxes/box-a", token=tok_a)[0], 404)
        self.assertEqual(self.req("GET", "/api/boxes/box-a", token=tok_b)[1]["name"], "Bar")
        self.assertEqual(self.sync()[1]["name"], "Bar")

    def test_command_lifecycle(self):
        club, tok = self.club_with_user("Club A", "clubby")
        self.linked_box(club)
        _, r = self.req("POST", "/api/boxes/box-a/command", {"kind": "restart"}, self.op)
        rid = r["commandId"]
        self.assertEqual(self.db("SELECT status FROM commands WHERE id=?", (rid,))[0][0], "queued")
        self.assertEqual(self.req("POST", "/api/boxes/box-a/command", {"kind": "rm -rf"}, self.op)[0], 400)
        for _ in range(2):  # the first response got lost: re-delivered until a result arrives
            self.assertEqual([c["id"] for c in self.sync(configRev=1)[1]["commands"]], [rid])
            self.assertEqual(self.db("SELECT status FROM commands WHERE id=?", (rid,))[0][0], "sent")
        self.sync(configRev=1, results=[{"id": rid, "ok": True, "output": "Herstart voltooid"}])
        self.sync(configRev=1, results=[{"id": rid, "ok": False, "output": "duplicate"}])  # idempotent
        self.assertEqual(self.db("SELECT status, ok, result FROM commands WHERE id=?", (rid,))[0],
                         ("done", 1, "Herstart voltooid"))
        self.assertEqual(self.sync(configRev=1)[1]["commands"], [])
        # credentials: a newer change replaces an undelivered one; payload wiped after the result
        self.req("POST", "/api/boxes/box-a/login", {"user": "ts", "password": "old-" + TS_PASS}, tok)
        self.req("POST", "/api/boxes/box-a/cookie", {"cookie": TS_COOKIE}, tok)
        _, r = self.req("POST", "/api/boxes/box-a/login", {"user": "ts", "password": TS_PASS}, tok)
        cmds = self.sync(configRev=1)[1]["commands"]
        self.assertEqual(cmds, [{"id": r["commandId"], "kind": "set_login", "payload": {"user": "ts", "pass": TS_PASS}}])
        self.sync(configRev=1, results=[{"id": r["commandId"], "ok": True, "output": "Ingelogd als TS"}])
        self.assertEqual(self.db("SELECT payload FROM commands WHERE id=?", (r["commandId"],))[0][0], None)
        self.assertEqual(self.req("POST", "/api/boxes/box-a/login", {"user": "ts"}, tok)[0], 400)

    def test_unlink_and_relink(self):
        club, _ = self.club_with_user("Club A", "clubby")
        self.linked_box(club)
        self.req("POST", "/api/boxes/box-a/command", {"kind": "logs"}, self.op)
        self.assertEqual(self.req("POST", "/api/boxes/box-a/unlink", {}, self.op)[0], 200)
        self.assertEqual(self.db("SELECT COUNT(*) FROM commands")[0][0], 0)
        self.assertEqual(self.req("GET", "/api/boxes/box-a", token=self.op)[0], 404)
        self.assertEqual(self.sync()[1], {"linked": False})
        self.assertEqual(self.sync(secret="fresh-" + "f" * 30, linkCode="NEW-COD")[1], {"linked": False})
        self.db("DELETE FROM pending WHERE secret_hash=?", (portal._sha(SECRET),))
        self.assertEqual(self.req("POST", "/api/boxes/link", {"code": "NEW-COD", "clubId": club}, self.op)[0], 200)
        self.assertTrue(self.sync(secret="fresh-" + "f" * 30)[1]["linked"])

    def test_secrets_never_returned(self):
        club, tok = self.club_with_user("Club A", "clubby")
        self.linked_box(club)
        self.req("POST", "/api/boxes/box-a/login", {"user": "ts", "password": TS_PASS}, tok)
        self.req("POST", "/api/boxes/box-a/cookie", {"cookie": TS_COOKIE}, tok)
        self.req("POST", "/api/boxes/box-a/command", {"kind": "logs"}, self.op)
        start = len(self.seen)
        for token in (self.op, tok):
            for path in ("/api/me", "/api/boxes", "/api/boxes/box-a", "/api/boxes/box-a/logs", "/api/clubs", "/api/users"):
                self.raw("GET", path, token=token)
        text = "\n".join(self.seen[start:])
        for secret in (TS_PASS, TS_COOKIE, SECRET, portal._sha(SECRET), "clubpass1", "adminpass1", "pass_hash", "salt"):
            self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
