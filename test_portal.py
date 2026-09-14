#!/usr/bin/env python3
"""Self-check for the hosted portal (portal/portal.py), against a real PostgreSQL.

Run: PORTAL_TEST_DATABASE_URL=postgresql://portal:portal@localhost:5432/portal python3 test_portal.py
Every test wipes that database's public schema.
"""

import base64
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import psycopg

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "portal"))
import portal  # noqa: E402

TEST_URL = os.environ.get("PORTAL_TEST_DATABASE_URL")
if not TEST_URL:
    sys.exit("PORTAL_TEST_DATABASE_URL is niet gezet — start bv. docker run --rm -e POSTGRES_PASSWORD=portal "
             "-e POSTGRES_USER=portal -e POSTGRES_DB=portal -p 5432:5432 postgres:16-alpine")

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
PNG_URL = "data:image/png;base64," + base64.b64encode(PNG).decode()
SECRET = "box-secret-" + "s" * 32
TS_PASS, TS_COOKIE = "hunter2tspass", "SECRETCOOKIEVALUE"


class PortalBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.saved = {k: getattr(portal, k) for k in ("DATABASE_URL", "UPLOADS_DIR", "LOGIN_DELAY")}
        portal.DATABASE_URL = TEST_URL
        portal.UPLOADS_DIR = os.path.join(self.tmp, "uploads")
        portal.LOGIN_DELAY = 0.2
        with psycopg.connect(TEST_URL) as c:  # fresh, empty database per test
            c.execute("DROP SCHEMA public CASCADE")
            c.execute("CREATE SCHEMA public")
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
    def raw(self, method, path, body=None, token=None, csrf=True, headers=None):
        headers = {"Content-Type": "application/json", **(headers or {})}
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

    def req(self, method, path, body=None, token=None, csrf=True, headers=None):
        status, _, text = self.raw(method, path, body, token, csrf, headers)
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
        with psycopg.connect(portal.DATABASE_URL) as c:
            cur = c.execute(sql, args)
            return cur.fetchall() if cur.description else []  # UPDATE/DELETE have no result set


class PortalTest(PortalBase):
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
                         {"username": "admin", "role": "operator", "clubId": None, "clubName": None, "acting": False})
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
        self.db("UPDATE pending SET last_seen=%s", (time.time() - 200,))  # stale code
        self.assertEqual(self.req("POST", "/api/boxes/link", {"code": "AB3-9KF", "clubId": club}, self.op)[0], 404)
        self.sync()
        # a second box (same id copied) reporting the same code → refuse, don't guess
        self.sync(secret="copycat-" + "c" * 30)
        self.assertEqual(self.req("POST", "/api/boxes/link", {"code": "AB3-9KF", "clubId": club}, self.op)[0], 409)
        self.db("DELETE FROM pending WHERE secret_hash<>%s", (portal._sha(SECRET),))
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

    def act_as_setup(self):
        """Clubs A (box-a) and B (box-b), then a second operator session acting as club A."""
        club_a = self.req("POST", "/api/clubs", {"name": "Club A"}, self.op)[1]["id"]
        club_b = self.req("POST", "/api/clubs", {"name": "Club B"}, self.op)[1]["id"]
        self.linked_box(club_a, "box-a", "AAA-AAA")
        self.linked_box(club_b, "box-b", "BBB-BBB", secret="second-" + "b" * 30)
        act = self.login("admin", "adminpass1")
        s, me = self.req("POST", "/api/act-as", {"clubId": club_a}, act)
        self.assertEqual((s, me), (200, {"username": "admin", "role": "club", "clubId": club_a,
                                         "clubName": "Club A", "acting": True}))
        return club_a, club_b, act

    def test_act_as_scoping(self):
        club_a, _, act = self.act_as_setup()
        self.assertEqual(self.req("GET", "/api/me", token=act)[1]["clubName"], "Club A")  # persists in the session
        self.assertTrue(self.req("GET", "/api/me", token=act)[1]["acting"])
        self.assertEqual([b["boxId"] for b in self.req("GET", "/api/boxes", token=act)[1]["boxes"]], ["box-a"])
        for method, path, body in (("GET", "/api/boxes/box-b", None),
                                   ("POST", "/api/boxes/box-b/display", {"display": {"clubName": "hack"}}),
                                   ("POST", "/api/boxes/box-b/tournaments", {"tournaments": []})):
            self.assertEqual(self.req(method, path, body, act)[0], 404, path)
        # the operator's other session is untouched
        self.assertEqual(self.req("GET", "/api/clubs", token=self.op)[0], 200)
        self.assertEqual(len(self.req("GET", "/api/boxes", token=self.op)[1]["boxes"]), 2)
        self.assertFalse(self.req("GET", "/api/me", token=self.op)[1]["acting"])
        # another club's logo preview: visible to the operator, not while acting
        self.assertEqual(self.req("POST", "/api/boxes/box-b/display", {"display": {"clubLogo": PNG_URL}}, self.op)[0], 200)
        preview = self.req("GET", "/api/boxes/box-b", token=self.op)[1]["logos"]["clubLogo"]["preview"]
        self.assertEqual(self.raw("GET", preview, token=self.op)[0], 200)
        self.assertEqual(self.raw("GET", preview, token=act)[0], 404)

    def test_act_as_operator_endpoints_refused(self):
        club_a, club_b, act = self.act_as_setup()
        self.req("POST", "/api/boxes/box-a/command", {"kind": "logs"}, self.op)
        self.req("POST", "/api/boxes/box-a/command", {"kind": "restart"}, self.op)
        for method, path, body in (("GET", "/api/clubs", None), ("GET", "/api/users", None), ("GET", "/api/leads", None),
                                   ("POST", "/api/boxes/link", {"code": "AAA-AAA", "clubId": club_a}),
                                   ("POST", "/api/boxes/box-a/command", {"kind": "restart"}),
                                   ("GET", "/api/boxes/box-a/logs", None),
                                   ("POST", "/api/boxes/box-a", {"clubId": club_b}),
                                   ("POST", "/api/boxes/box-a/unlink", {})):
            self.assertEqual(self.req(method, path, body, act)[0], 403, path)
        self.assertEqual(self.db("SELECT COUNT(*) FROM commands")[0][0], 2)
        before = self.db("SELECT pass_hash FROM users WHERE username='admin'")
        self.assertEqual(self.req("POST", "/api/password", {"old": "adminpass1", "new": "otherpass1"}, act)[0], 403)
        self.assertEqual(self.db("SELECT pass_hash FROM users WHERE username='admin'"), before)
        d = self.req("GET", "/api/boxes/box-a", token=act)[1]
        self.assertIsNone(d["logsAt"])
        self.assertFalse({c["kind"] for c in d["commands"]} & set(portal.BOX_COMMANDS))
        self.assertEqual({c["kind"] for c in self.req("GET", "/api/boxes/box-a", token=self.op)[1]["commands"]},
                         {"logs", "restart"})

    def test_act_as_club_edits_work(self):
        _, _, act = self.act_as_setup()
        for path, body in (("display", {"display": {"clubName": "Via beheer"}}),
                           ("tournaments", {"tournaments": [{"code": "t7", "label": "Zomer"}]})):
            s, out = self.req("POST", "/api/boxes/box-a/" + path, body, act)
            self.assertEqual(s, 200, (path, out))
        desired = json.loads(self.db("SELECT desired FROM boxes WHERE box_id='box-a'")[0][0])
        self.assertEqual(desired["display"]["clubName"], "Via beheer")
        self.assertEqual(desired["tournaments"][0]["code"], "t7")
        # a newer credential command replaces the queued one, so check each right after it lands
        for path, body, kind in (("login", {"user": "ts", "password": TS_PASS}, "set_login"),
                                 ("cookie", {"cookie": TS_COOKIE}, "set_cookie")):
            s, out = self.req("POST", "/api/boxes/box-a/" + path, body, act)
            self.assertEqual(s, 200, (path, out))
            self.assertEqual([r[0] for r in self.db("SELECT kind FROM commands WHERE box_id='box-a'")], [kind])

    def test_act_as_stop_and_club_user_refused(self):
        club_a, club_b, act = self.act_as_setup()
        s, me = self.req("POST", "/api/act-as", {"clubId": None}, act)
        self.assertEqual((s, me), (200, {"username": "admin", "role": "operator", "clubId": None,
                                         "clubName": None, "acting": False}))
        self.assertEqual(self.req("GET", "/api/clubs", token=act)[0], 200)
        self.assertEqual(len(self.req("GET", "/api/boxes", token=act)[1]["boxes"]), 2)
        # bad input never silently stops or starts acting
        self.assertEqual(self.req("POST", "/api/act-as", {"clubId": club_a}, act)[0], 200)
        for bad in (999999, "abc"):
            self.assertEqual(self.req("POST", "/api/act-as", {"clubId": bad}, act)[0], 400)
        self.assertEqual(self.req("GET", "/api/me", token=act)[1]["clubId"], club_a)
        self.assertEqual(self.req("POST", "/api/act-as", {}, act)[1]["acting"], False)
        # a club without users or boxes can be entered
        empty = self.req("POST", "/api/clubs", {"name": "Leeg"}, self.op)[1]["id"]
        self.assertEqual(self.req("POST", "/api/act-as", {"clubId": empty}, act)[1]["clubName"], "Leeg")
        self.assertEqual(self.req("GET", "/api/boxes", token=act)[1], {"boxes": []})
        # club users can neither start nor stop
        _, tok = self.club_with_user("Club C", "userc")
        for body in ({"clubId": club_b}, {"clubId": None}, {}):
            self.assertEqual(self.req("POST", "/api/act-as", body, tok)[0], 403)
        self.assertEqual(self.req("GET", "/api/me", token=tok)[1]["clubName"], "Club C")
        self.assertEqual(self.req("GET", "/api/boxes", token=tok)[1], {"boxes": []})
        # logout ends it with the session
        self.assertEqual(self.req("POST", "/api/logout", {}, act)[0], 200)
        self.assertEqual(self.req("GET", "/api/me", token=act)[0], 401)

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
        self.assertEqual(self.db("SELECT status FROM commands WHERE id=%s", (rid,))[0][0], "queued")
        self.assertEqual(self.req("POST", "/api/boxes/box-a/command", {"kind": "rm -rf"}, self.op)[0], 400)
        for _ in range(2):  # the first response got lost: re-delivered until a result arrives
            self.assertEqual([c["id"] for c in self.sync(configRev=1)[1]["commands"]], [rid])
            self.assertEqual(self.db("SELECT status FROM commands WHERE id=%s", (rid,))[0][0], "sent")
        self.sync(configRev=1, results=[{"id": rid, "ok": True, "output": "Herstart voltooid"}])
        self.sync(configRev=1, results=[{"id": rid, "ok": False, "output": "duplicate"}])  # idempotent
        self.assertEqual(self.db("SELECT status, ok, result FROM commands WHERE id=%s", (rid,))[0],
                         ("done", 1, "Herstart voltooid"))
        self.assertEqual(self.sync(configRev=1)[1]["commands"], [])
        # credentials: a newer change replaces an undelivered one; payload wiped after the result
        self.req("POST", "/api/boxes/box-a/login", {"user": "ts", "password": "old-" + TS_PASS}, tok)
        self.req("POST", "/api/boxes/box-a/cookie", {"cookie": TS_COOKIE}, tok)
        _, r = self.req("POST", "/api/boxes/box-a/login", {"user": "ts", "password": TS_PASS}, tok)
        cmds = self.sync(configRev=1)[1]["commands"]
        self.assertEqual(cmds, [{"id": r["commandId"], "kind": "set_login", "payload": {"user": "ts", "pass": TS_PASS}}])
        self.sync(configRev=1, results=[{"id": r["commandId"], "ok": True, "output": "Ingelogd als TS"}])
        self.assertEqual(self.db("SELECT payload FROM commands WHERE id=%s", (r["commandId"],))[0][0], None)
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
        self.db("DELETE FROM pending WHERE secret_hash=%s", (portal._sha(SECRET),))
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


LEAD = {"name": "Jan Jansen", "club": "TC Oost", "email": "jan@tcoost.nl", "message": "8 banen\nvolgende maand"}
LEAD_ERROR = "Vul je naam en een geldig e-mailadres in."


class LeadsTest(PortalBase):
    def setUp(self):
        super().setUp()
        for k in ("notify_lead", "_notify_async", "SMTP_HOST", "SMTP_PORT", "LEAD_TO"):
            self.saved[k] = getattr(portal, k)
        self.real_notify = portal.notify_lead
        self.mailed, self.mail_result, self.ip = [], True, 0

        def fake_notify(lead):
            self.mailed.append(lead)
            return self.mail_result
        portal.notify_lead = fake_notify
        portal._notify_async = portal._record_notify  # synchronous: no thread writing after cleanup
        portal._demo_hits.clear()

    def demo(self, body, ip=None, **kw):
        self.ip += 1  # own source per request, so only the throttle test hits the limit
        return self.req("POST", "/api/demo", body, headers={"X-Forwarded-For": ip or "10.0.0.%d" % self.ip}, **kw)

    def leads(self):
        s, out = self.req("GET", "/api/leads", token=self.op)
        self.assertEqual(s, 200)
        return out["leads"]

    def test_site_at_root_portal_at_portal(self):
        for path in ("/", "/index.html"):
            status, hdrs, text = self.raw("GET", path)
            self.assertEqual(status, 200, path)
            for s in ("Plan een demo", "Inloggen", 'href="/portal"', "Vul je naam en een geldig e-mailadres in.", "novalidate"):
                self.assertIn(s.encode(), text, s)
            self.assertIn("frame-ancestors 'none'", hdrs["Content-Security-Policy"])
        for path in ("/portal", "/portal/"):
            status, _, text = self.raw("GET", path)
            self.assertEqual(status, 200, path)
            self.assertIn("Toernooi TV · Portaal".encode(), text)
        self.assertEqual(self.raw("GET", "/portal.html")[0], 404)

    def test_valid_request_stored_and_mailed(self):
        self.assertEqual(self.demo(LEAD), (200, {"ok": True}))
        self.assertEqual([(m["name"], m["club"], m["email"], m["message"]) for m in self.mailed],
                         [("Jan Jansen", "TC Oost", "jan@tcoost.nl", "8 banen\nvolgende maand")])
        self.mail_result = None  # SMTP not configured
        self.assertEqual(self.demo({"name": "  Piet\r\n Puk ", "email": " piet@club.nl "})[0], 200)
        leads = self.leads()
        self.assertEqual([(l["name"], l["club"], l["email"], l["emailed"]) for l in leads],
                         [("Piet Puk", "", "piet@club.nl", None), ("Jan Jansen", "TC Oost", "jan@tcoost.nl", True)])
        self.assertEqual(leads[1]["message"], "8 banen\nvolgende maand")
        self.assertAlmostEqual(leads[1]["created"], time.time(), delta=60)

    def test_invalid_input_stores_nothing(self):
        for body in ({"email": "jan@tcoost.nl"}, {"name": "   ", "email": "jan@tcoost.nl"},
                     {"name": "Jan", "email": "foo@bar"}, {"name": "Jan", "email": ""}, {"name": "Jan"},
                     {"name": "Jan", "email": "jan jansen@tcoost.nl"}, {"name": "Jan", "email": "a@b.nl\r\nBcc: x@y.nl"}):
            self.assertEqual(self.demo(body), (400, {"ok": False, "error": LEAD_ERROR}), body)
        self.assertEqual(self.demo(LEAD, csrf=False)[0], 403)
        self.assertEqual(self.demo({**LEAD, "message": "x" * 20000})[0], 413)
        self.assertEqual((self.leads(), self.mailed), ([], []))

    def test_mail_failure_still_stored(self):
        self.mail_result = False
        self.assertEqual(self.demo(LEAD), (200, {"ok": True}))
        self.assertEqual(self.leads()[0]["emailed"], False)
        # the real SMTP path against a closed port
        portal.notify_lead = self.real_notify
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        portal.SMTP_HOST, portal.SMTP_PORT = "127.0.0.1", port
        self.assertEqual(self.demo({**LEAD, "name": "Kees"}), (200, {"ok": True}))
        self.assertEqual([(l["name"], l["emailed"]) for l in self.leads()], [("Kees", False), ("Jan Jansen", False)])

    def test_async_notify_updates_lead(self):
        portal._notify_async = self.saved["_notify_async"]
        done = threading.Event()
        portal.notify_lead = lambda lead: done.set() or True
        self.assertEqual(self.demo(LEAD)[0], 200)
        self.assertTrue(done.wait(5))
        for _ in range(50):
            if self.leads()[0]["emailed"]:
                break
            time.sleep(0.05)
        self.assertEqual(self.leads()[0]["emailed"], True)

    def test_leads_operator_only(self):
        self.demo(LEAD)
        _, tok = self.club_with_user("Club A", "clubby")
        self.assertEqual(self.req("GET", "/api/leads", token=tok), (403, {"ok": False, "error": "Alleen voor de beheerder"}))
        self.assertEqual(self.req("GET", "/api/leads")[0], 401)
        self.assertNotIn("jan@tcoost.nl", "\n".join(self.seen[-2:]))
        self.assertEqual(len(self.leads()), 1)

    def test_honeypot(self):
        self.assertEqual(self.demo({**LEAD, "website": "http://spam.example"}), (200, {"ok": True}))
        self.assertEqual((self.leads(), self.mailed), ([], []))

    def test_throttle_per_ip(self):
        for _ in range(portal.DEMO_LIMIT):
            self.assertEqual(self.demo(LEAD, ip="203.0.113.9")[0], 200)
        self.assertEqual(self.demo(LEAD, ip="203.0.113.9")[0], 429)
        # a spoofed first X-Forwarded-For entry doesn't help: the proxy appends the real client last
        self.assertEqual(self.demo(LEAD, ip="1.2.3.4, 203.0.113.9")[0], 429)
        self.assertEqual(self.demo(LEAD, ip="198.51.100.7")[0], 200)
        self.assertEqual(len(self.leads()), portal.DEMO_LIMIT + 1)
        portal._demo_hits["203.0.113.9"] = [time.time() - portal.DEMO_WINDOW - 1] * portal.DEMO_LIMIT
        self.assertEqual(self.demo(LEAD, ip="203.0.113.9")[0], 200)  # window passed

    def test_client_ip_trusts_forwarded_only_from_private_peer(self):
        def ip(peer):
            stub = type("H", (), {"client_address": (peer, 1), "headers": {"X-Forwarded-For": "1.2.3.4, 203.0.113.9"}})
            return portal._client_ip(stub)
        self.assertEqual(ip("172.18.0.5"), "203.0.113.9")  # proxy container on the Docker network
        self.assertEqual(ip("8.8.8.8"), "8.8.8.8")  # public peer can't spoof its address (203.0.113.x counts as private)

    def test_notify_lead_unconfigured(self):
        portal.SMTP_HOST = ""
        self.assertIsNone(self.real_notify({**LEAD, "created": time.time()}))

    def test_notify_lead_message(self):
        portal.SMTP_HOST, portal.SMTP_PORT = "smtp.test", 587
        with unittest.mock.patch.object(portal.smtplib, "SMTP") as smtp:
            self.assertTrue(self.real_notify({**LEAD, "created": time.time()}))
        smtp.assert_called_once_with("smtp.test", 587, timeout=15)
        conn = smtp.return_value
        conn.send_message.assert_called_once()
        msg = conn.send_message.call_args[0][0]
        self.assertEqual((msg["To"], msg["Reply-To"]), (portal.LEAD_TO, "jan@tcoost.nl"))
        self.assertIn("Jan Jansen (TC Oost)", msg["Subject"])
        self.assertIn("8 banen", msg.get_content())
        with unittest.mock.patch.object(portal.smtplib, "SMTP", side_effect=OSError("down")):
            self.assertFalse(self.real_notify({**LEAD, "created": time.time()}))


if __name__ == "__main__":
    unittest.main()
