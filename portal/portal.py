#!/usr/bin/env python3
"""
Toernooi TV portaal — hosted remote management for Toernooi TV boxes.

Boxes connect OUTWARD (POST /api/box/sync every 10 s, see server.py), so they
work behind any club NAT. Operators and club users manage them from the Dutch
web UI (portal.html). Stdlib only: ThreadingHTTPServer + sqlite3. Run it behind
a TLS reverse proxy (Caddy/nginx) — see README "Portaal".

Run:
    python3 portal/portal.py --create-operator <username>   # once; also resets its password
    python3 portal/portal.py                                  # serves 127.0.0.1:8771

Env: PORTAL_HOST, PORTAL_PORT, PORTAL_DB (default portal/portal.db),
     PORTAL_COOKIE_SECURE=0 to allow the session cookie over plain http (dev only),
     PORTAL_LEAD_TO (default info@toernooitv.nl), PORTAL_LEAD_FROM, PORTAL_SMTP_HOST
     (empty = demo requests are stored but not mailed), PORTAL_SMTP_PORT (587; 465 = SSL),
     PORTAL_SMTP_USER, PORTAL_SMTP_PASS, PORTAL_SMTP_STARTTLS=0 to skip STARTTLS.

The public website (site.html) is served at /, the management UI at /portal.
"""

import base64
import binascii
import contextlib
import getpass
import glob
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import smtplib
import sqlite3
import sys
import threading
import time
import traceback
import urllib.parse
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(HERE, "portal.html")
SITE_PATH = os.path.join(HERE, "site.html")
VENDOR_DIR = os.path.join(os.path.dirname(HERE), "vendor")
UPLOADS_DIR = os.path.join(HERE, "uploads")  # portal-side logo previews
DB_PATH = os.environ.get("PORTAL_DB", os.path.join(HERE, "portal.db"))
HOST = os.environ.get("PORTAL_HOST", "127.0.0.1")
PORT = int(os.environ.get("PORTAL_PORT", "8771"))
COOKIE_SECURE = os.environ.get("PORTAL_COOKIE_SECURE", "1") != "0"
LEAD_TO = os.environ.get("PORTAL_LEAD_TO", "info@toernooitv.nl")
SMTP_HOST = os.environ.get("PORTAL_SMTP_HOST", "")  # empty = don't send (leads still stored)
SMTP_PORT = int(os.environ.get("PORTAL_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("PORTAL_SMTP_USER", "")
SMTP_PASS = os.environ.get("PORTAL_SMTP_PASS", "")
SMTP_STARTTLS = os.environ.get("PORTAL_SMTP_STARTTLS", "1") != "0"
LEAD_FROM = os.environ.get("PORTAL_LEAD_FROM", SMTP_USER or "noreply@toernooitv.nl")
DEMO_LIMIT, DEMO_WINDOW = 3, 900  # max 3 demo requests per IP per 15 min
DEMO_MAX_BODY = 16384

SESSION_COOKIE = "portal_session"
SESSION_TTL = 30 * 86400
ONLINE_SEC = 45        # a sync cycle can take >30 s (TS login, probes, logo downloads)
LINK_WINDOW = 120      # a link code must have been reported this recently
LOGIN_DELAY = 1.0      # seconds, on every failed login
MAX_BODY = 15 * 1024 * 1024
MAX_DATA_URL = 7_000_000  # 5 MB raw ≈ 6.7 M base64 chars
LOGO_MAX_BYTES = 5 * 1024 * 1024
LOGO_FIELDS = {"clubLogo": "club", "sponsorLogo": "sponsor"}
LOGO_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/svg+xml": "svg",
            "image/webp": "webp", "image/gif": "gif"}
BOX_COMMANDS = ("restart", "reboot", "update", "logs")  # operator only
CRED_COMMANDS = ("set_login", "clear_login", "set_cookie", "clear_cookie")

mimetypes.add_type("font/woff2", ".woff2")
_db_lock = threading.Lock()  # ponytail: global DB write lock — fine for one process; per-box locks if write volume grows

SCHEMA = """
CREATE TABLE IF NOT EXISTS clubs(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, pass_hash TEXT NOT NULL, salt TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('operator','club')), club_id INTEGER REFERENCES clubs(id));
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS pending(
  secret_hash TEXT PRIMARY KEY, box_id TEXT NOT NULL, link_code TEXT, snapshot TEXT, last_seen REAL);
CREATE TABLE IF NOT EXISTS boxes(
  box_id TEXT PRIMARY KEY, secret_hash TEXT NOT NULL, club_id INTEGER NOT NULL REFERENCES clubs(id),
  name TEXT DEFAULT '', last_seen REAL, snapshot TEXT, desired TEXT, rev INTEGER DEFAULT 0,
  previews TEXT DEFAULT '{}', logs TEXT, logs_at REAL, created REAL);
CREATE TABLE IF NOT EXISTS commands(
  id INTEGER PRIMARY KEY AUTOINCREMENT, box_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT,
  status TEXT DEFAULT 'queued', result TEXT, ok INTEGER, created REAL, sent_at REAL, done_at REAL);
CREATE INDEX IF NOT EXISTS commands_box ON commands(box_id, status);
CREATE TABLE IF NOT EXISTS leads(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, club TEXT DEFAULT '', email TEXT NOT NULL,
  message TEXT DEFAULT '', created REAL,
  emailed INTEGER);  -- NULL = no mail sent (not configured / pending), 1 = sent, 0 = failed
"""


class Fail(Exception):
    """An API error with an HTTP status and a Dutch message."""

    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


@contextlib.contextmanager
def _db():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    try:
        with c:
            yield c
    finally:
        c.close()


def init_db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with _db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)


def _sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _j(text, default):
    try:
        v = json.loads(text) if text else default
    except ValueError:
        return default
    return v if isinstance(v, type(default)) else default


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _norm_code(code):
    return re.sub(r"[^A-Z0-9]", "", str(code or "").upper())


# ---- passwords & users ------------------------------------------------------
def hash_password(pw, salt_hex=None):
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    return hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1).hex(), salt.hex()


def check_password(row, pw):
    return hmac.compare_digest(hash_password(pw, row["salt"])[0], row["pass_hash"])


def _check_new_password(pw):
    if not isinstance(pw, str) or len(pw) < 8:
        raise Fail(400, "Wachtwoord moet minstens 8 tekens hebben")


def set_operator(username, password):
    """Create the operator account, or reset its password (bootstrap CLI)."""
    username = (username or "").strip()
    if not re.fullmatch(r"[\w.@+-]{3,64}", username):
        raise Fail(400, "Ongeldige gebruikersnaam")
    _check_new_password(password)
    h, salt = hash_password(password)
    with _db_lock, _db() as c:
        row = c.execute("SELECT id, role FROM users WHERE username=?", (username,)).fetchone()
        if row and row["role"] != "operator":
            raise Fail(400, "%s is een clubgebruiker" % username)
        if row:
            c.execute("UPDATE users SET pass_hash=?, salt=? WHERE id=?", (h, salt, row["id"]))
            c.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
        else:
            c.execute("INSERT INTO users(username, pass_hash, salt, role) VALUES (?,?,?,'operator')",
                      (username, h, salt))


# ---- boxes ------------------------------------------------------------------
def _visible_box(c, user, box_id):
    """Operator: any linked box. Club user: only their club's — else 404."""
    row = c.execute("SELECT b.*, cl.name AS club_name FROM boxes b LEFT JOIN clubs cl ON cl.id=b.club_id "
                    "WHERE b.box_id=?", (box_id,)).fetchone()
    if not row or (user["role"] != "operator" and row["club_id"] != user["club_id"]):
        raise Fail(404, "Box niet gevonden")
    return row


def _box_summary(row):
    snap = _j(row["snapshot"], {})
    st = snap.get("status") if isinstance(snap.get("status"), dict) else {}
    return {
        "boxId": row["box_id"], "name": row["name"], "clubId": row["club_id"], "clubName": row["club_name"],
        "online": bool(row["last_seen"]) and time.time() - row["last_seen"] < ONLINE_SEC,
        "lastSeen": row["last_seen"], "version": snap.get("version", ""),
        "ssid": st.get("ssid", ""), "ip": st.get("ip", ""), "internet": st.get("online"),
        "setupMode": bool(st.get("setupMode")), "hotspotName": st.get("hotspotName", ""),
        "auth": snap.get("auth") or {}, "update": snap.get("update"),
        "synced": snap.get("configRev") == row["rev"],
    }


def _remove_preview(box_id, field):
    for p in glob.glob(os.path.join(UPLOADS_DIR, "%s-%s.*" % (box_id, LOGO_FIELDS[field]))):
        try:
            os.remove(p)
        except OSError:
            pass


def _save_preview(box_id, field, data_url):
    """Keep an uploaded logo portal-side so the UI can preview it; returns its URL."""
    m = re.match(r"data:([\w.+/-]+);base64,(.*)\Z", data_url, re.S)
    if not m or m.group(1).lower() not in LOGO_EXT:
        raise Fail(400, "Logo: alleen PNG, JPG, SVG, WebP of GIF")
    try:
        raw = base64.b64decode(m.group(2), validate=True)
    except (binascii.Error, ValueError):
        raise Fail(400, "Logo: ongeldige afbeelding") from None
    if len(raw) > LOGO_MAX_BYTES:
        raise Fail(413, "Logo te groot (max 5 MB)")
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    _remove_preview(box_id, field)
    name = "%s-%s.%s" % (box_id, LOGO_FIELDS[field], LOGO_EXT[m.group(1).lower()])
    path = os.path.join(UPLOADS_DIR, name)
    with open(path + ".tmp", "wb") as f:
        f.write(raw)
    os.replace(path + ".tmp", path)
    return "/uploads/%s?v=%d" % (name, int(time.time() * 1000))


def _queue(c, box_id, kind, payload=None):
    if kind in CRED_COMMANDS:  # a newer credential change replaces any not yet delivered
        c.execute("DELETE FROM commands WHERE box_id=? AND status='queued' AND kind IN (?,?,?,?)",
                  (box_id,) + CRED_COMMANDS)
    cur = c.execute("INSERT INTO commands(box_id, kind, payload, created) VALUES (?,?,?,?)",
                    (box_id, kind, json.dumps(payload) if payload else None, time.time()))
    return {"ok": True, "commandId": cur.lastrowid}


# ---- demo requests (public website) -----------------------------------------
_demo_hits = {}  # ip -> [timestamps]
_demo_lock = threading.Lock()


def _throttle(ip):
    # ponytail: in-memory per-IP window, resets on restart — persist to sqlite if abuse ever outlives restarts
    now = time.time()
    with _demo_lock:
        for k in list(_demo_hits):
            _demo_hits[k] = [t for t in _demo_hits[k] if now - t < DEMO_WINDOW]
            if not _demo_hits[k]:
                del _demo_hits[k]
        hits = _demo_hits.setdefault(ip, [])
        if len(hits) >= DEMO_LIMIT:
            raise Fail(429, "Te veel aanvragen — probeer het later nog eens")
        hits.append(now)


def _client_ip(handler):
    ip = handler.client_address[0]
    fwd = handler.headers.get("X-Forwarded-For")
    if fwd and ip in ("127.0.0.1", "::1"):  # via the local reverse proxy: it appends the real client last
        return fwd.split(",")[-1].strip() or ip
    return ip


def notify_lead(lead):
    """Mail a demo request to LEAD_TO. True = sent, False = failed, None = SMTP not configured. Never raises."""
    if not SMTP_HOST:
        print("PORTAL_SMTP_HOST leeg — geen mail verstuurd voor demo-aanvraag")
        return None
    try:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Reply-To"] = LEAD_FROM, LEAD_TO, lead["email"]
        msg["Subject"] = "Nieuwe demo-aanvraag — %s%s" % (lead["name"], " (%s)" % lead["club"] if lead["club"] else "")
        msg.set_content("Naam: %s\nClub: %s\nE-mail: %s\nTijdstip: %s\n\nBericht:\n%s\n" % (
            lead["name"], lead["club"] or "-", lead["email"],
            time.strftime("%d-%m-%Y %H:%M", time.localtime(lead["created"])), lead["message"] or "-"))
        if SMTP_PORT == 465:
            smtp = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15)
        else:
            smtp = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
        with smtp:
            if SMTP_PORT != 465 and SMTP_STARTTLS:
                smtp.starttls()
            if SMTP_USER:
                smtp.login(SMTP_USER, SMTP_PASS)
            smtp.send_message(msg)
        return True
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return False


def _record_notify(lead_id, lead):
    sent = notify_lead(lead)
    if sent is not None:
        with _db_lock, _db() as c:
            c.execute("UPDATE leads SET emailed=? WHERE id=?", (1 if sent else 0, lead_id))


def _notify_async(lead_id, lead):
    # the visitor never waits on (or fails because of) the mail server
    threading.Thread(target=_record_notify, args=(lead_id, lead), daemon=True).start()


# ---- HTTP -------------------------------------------------------------------
ROUTES = [  # (method, path regex, handler, who: None = public, "user", "operator")
    ("POST", r"/api/box/sync", "box_sync", None),
    ("POST", r"/api/login", "login", None),
    ("POST", r"/api/logout", "logout", "user"),
    ("GET", r"/api/me", "me", "user"),
    ("POST", r"/api/password", "password", "user"),
    ("GET", r"/api/boxes", "boxes", "user"),
    ("POST", r"/api/boxes/link", "link", "operator"),
    ("GET", r"/api/boxes/([\w-]+)", "box_detail", "user"),
    ("POST", r"/api/boxes/([\w-]+)", "box_edit", "operator"),
    ("POST", r"/api/boxes/([\w-]+)/unlink", "unlink", "operator"),
    ("POST", r"/api/boxes/([\w-]+)/display", "display", "user"),
    ("POST", r"/api/boxes/([\w-]+)/tournaments", "tournaments", "user"),
    ("POST", r"/api/boxes/([\w-]+)/login", "ts_login", "user"),
    ("POST", r"/api/boxes/([\w-]+)/cookie", "ts_cookie", "user"),
    ("POST", r"/api/boxes/([\w-]+)/command", "command", "operator"),
    ("GET", r"/api/boxes/([\w-]+)/logs", "logs", "operator"),
    ("GET", r"/api/clubs", "clubs", "operator"),
    ("POST", r"/api/clubs", "club_create", "operator"),
    ("DELETE", r"/api/clubs/(\d+)", "club_delete", "operator"),
    ("GET", r"/api/users", "users", "operator"),
    ("POST", r"/api/users", "user_create", "operator"),
    ("DELETE", r"/api/users/(\d+)", "user_delete", "operator"),
    ("POST", r"/api/users/(\d+)/password", "user_password", "operator"),
    ("POST", r"/api/demo", "demo", None),
    ("GET", r"/api/leads", "leads", "operator"),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "ToernooiTVPortaal"

    def log_message(self, fmt, *args):
        if args and "/api/box/sync" in str(args[0]):
            return  # every box, every 10 s — too noisy for the journal
        super().log_message(fmt, *args)

    # -- plumbing --
    def _send(self, code, body, ctype, headers=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200, headers=()):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", [("Cache-Control", "no-store"), *headers])

    def _file(self, path, headers=()):
        with open(path, "rb") as f:
            data = f.read()
        self._send(200, data, mimetypes.guess_type(path)[0] or "application/octet-stream", headers)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def _handle(self, method):
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path.startswith("/api/"):
                self._api(method, path)
            elif method == "GET":
                self._static(path)
            else:
                raise Fail(404, "Niet gevonden")
        except Fail as e:
            self._json({"ok": False, "error": e.msg}, e.code)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._json({"ok": False, "error": "Serverfout"}, 500)

    def _token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == SESSION_COOKIE and v:
                return v
        return ""

    def _user(self):
        tok = self._token()
        if tok:
            with _db() as c:
                row = c.execute(
                    "SELECT u.id, u.username, u.role, u.club_id, cl.name AS club_name FROM sessions s "
                    "JOIN users u ON u.id=s.user_id LEFT JOIN clubs cl ON cl.id=u.club_id "
                    "WHERE s.token=? AND s.expires>?", (_sha(tok), time.time())).fetchone()
            if row:
                return {**dict(row), "session": _sha(tok)}
        raise Fail(401, "Niet ingelogd")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise Fail(413, "Verzoek te groot (max 15 MB)")
        if not n:
            return {}
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise Fail(400, "Ongeldige JSON") from None
        if not isinstance(data, dict):
            raise Fail(400, "Ongeldige JSON")
        return data

    def _session_cookie(self, token, ttl):
        return "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d%s" % (
            SESSION_COOKIE, token, ttl, "; Secure" if COOKIE_SECURE else "")

    def _api(self, method, path):
        for m, pattern, fn, who in ROUTES:
            match = re.fullmatch(pattern, path)
            if match and m == method:
                break
        else:
            raise Fail(404, "Onbekend endpoint")
        # CSRF: browsers can't add custom headers cross-site without CORS
        if method != "GET" and fn not in ("box_sync", "login") and self.headers.get("X-Portal") != "1":
            raise Fail(403, "X-Portal-header ontbreekt")
        user = None
        if who:
            user = self._user()
            if who == "operator" and user["role"] != "operator":
                raise Fail(403, "Alleen voor de beheerder")
        if fn == "demo" and int(self.headers.get("Content-Length") or 0) > DEMO_MAX_BODY:
            raise Fail(413, "Verzoek te groot")
        body =self._body() if method != "GET" else {}
        out = getattr(self, "api_" + fn)(user, body, *match.groups())
        obj, headers = out if isinstance(out, tuple) else (out, ())
        self._json(obj, 200, headers)

    def _static(self, path):
        page = SITE_PATH if path in ("/", "/index.html") else HTML_PATH if path in ("/portal", "/portal/") else None
        if page:
            return self._file(page, [
                ("Cache-Control", "no-store"),
                ("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' "
                 "'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")])
        for prefix, root in (("/vendor/", VENDOR_DIR), ("/uploads/", UPLOADS_DIR)):
            if not path.startswith(prefix):
                continue
            user = self._user() if prefix == "/uploads/" else None  # login before revealing anything
            root = os.path.realpath(root)
            full = os.path.realpath(os.path.join(root, urllib.parse.unquote(path[len(prefix):])))
            if not full.startswith(root + os.sep) or not os.path.isfile(full):
                break
            if prefix == "/vendor/":
                return self._file(full, [("Cache-Control", "public, max-age=86400")])
            m = re.fullmatch(r"([\w-]+)-(club|sponsor)\.\w+", os.path.basename(full))
            if not m:
                break
            with _db() as c:
                _visible_box(c, user, m.group(1))
            return self._file(full, [
                ("Cache-Control", "private, max-age=3600"),
                ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; sandbox")])
        raise Fail(404, "Niet gevonden")

    # -- box sync --
    def api_box_sync(self, _user, body):
        box_id, secret = str(body.get("boxId") or ""), body.get("secret")
        if not re.fullmatch(r"[\w-]{1,64}", box_id) or not isinstance(secret, str) or len(secret) < 20:
            raise Fail(400, "Ongeldige box")
        sh, now = _sha(secret), time.time()
        snap = {k: v for k, v in body.items() if k not in ("secret", "results")}
        with _db_lock, _db() as c:
            row = c.execute("SELECT * FROM boxes WHERE box_id=?", (box_id,)).fetchone()
            if row is None:
                # unlinked: remember it by its secret so the operator can link it by
                # code. Never 401 here — a stranger can't block the real box.
                c.execute("DELETE FROM pending WHERE last_seen<?", (now - 86400,))
                c.execute("INSERT INTO pending(secret_hash, box_id, link_code, snapshot, last_seen) "
                          "VALUES (?,?,?,?,?) ON CONFLICT(secret_hash) DO UPDATE SET box_id=excluded.box_id, "
                          "link_code=excluded.link_code, snapshot=excluded.snapshot, last_seen=excluded.last_seen",
                          (sh, box_id, _norm_code(body.get("linkCode")), json.dumps(snap), now))
                return {"linked": False}
            if not hmac.compare_digest(row["secret_hash"], sh):
                raise Fail(401, "Box niet herkend")

            for r in body.get("results") or []:
                cid = _int(r.get("id")) if isinstance(r, dict) else None
                cmd = c.execute("SELECT kind, status FROM commands WHERE id=? AND box_id=?",
                                (cid, box_id)).fetchone()
                if not cmd or cmd["status"] == "done":
                    continue  # duplicate result (resent after a lost response)
                output = str(r.get("output") or "")[:300000]
                if cmd["kind"] == "logs":  # logs live on the box row (operator-only endpoint)
                    c.execute("UPDATE boxes SET logs=?, logs_at=? WHERE box_id=?", (output, now, box_id))
                    output = None
                c.execute("UPDATE commands SET status='done', ok=?, result=?, payload=NULL, done_at=? WHERE id=?",
                          (1 if r.get("ok") else 0, output, now, cid))

            rev, box_rev = row["rev"], _int(body.get("configRev")) or 0
            desired = _j(row["desired"], {})
            if box_rev == rev and isinstance(body.get("display"), dict):
                # settle: the box applied this revision — adopt its effective (cleaned) values
                desired = {"display": body["display"],
                           "tournaments": body.get("tournaments") if isinstance(body.get("tournaments"), list) else []}
                previews = _j(row["previews"], {})
                logo_warning = any("logo" in str(w) for w in body.get("configWarnings") or [])
                for k in LOGO_FIELDS:
                    if k in previews and (logo_warning or not body["display"].get(k)):
                        previews.pop(k)
                        _remove_preview(box_id, k)
                c.execute("UPDATE boxes SET desired=?, previews=? WHERE box_id=? AND rev=?",
                          (json.dumps(desired), json.dumps(previews), box_id, rev))
            c.execute("UPDATE boxes SET last_seen=?, snapshot=? WHERE box_id=?", (now, json.dumps(snap), box_id))
            # every undelivered or unconfirmed command, until its result arrives
            cmds = [{"id": r["id"], "kind": r["kind"], "payload": _j(r["payload"], {})} for r in c.execute(
                "SELECT id, kind, payload FROM commands WHERE box_id=? AND status IN ('queued','sent') ORDER BY id",
                (box_id,))]
            c.execute("UPDATE commands SET status='sent', sent_at=? WHERE box_id=? AND status='queued'", (now, box_id))
        out = {"linked": True, "name": row["name"], "configRev": rev, "commands": cmds}
        if box_rev != rev:
            out["config"] = {"display": desired.get("display") or {}, "tournaments": desired.get("tournaments") or []}
        return out

    # -- session --
    def api_login(self, _user, body):
        username, pw = str(body.get("username") or "").strip(), body.get("password")
        with _db() as c:
            row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        ok = isinstance(pw, str) and row is not None and check_password(row, pw)
        if not ok:
            time.sleep(LOGIN_DELAY)
            raise Fail(401, "Onjuiste gebruikersnaam of wachtwoord")
        token, now = secrets.token_urlsafe(32), time.time()
        with _db_lock, _db() as c:
            c.execute("DELETE FROM sessions WHERE expires<?", (now,))
            c.execute("INSERT INTO sessions(token, user_id, expires) VALUES (?,?,?)",
                      (_sha(token), row["id"], now + SESSION_TTL))
            club = c.execute("SELECT name FROM clubs WHERE id=?", (row["club_id"],)).fetchone()
        me = {"username": row["username"], "role": row["role"], "clubId": row["club_id"],
              "clubName": club["name"] if club else None}
        return me, [("Set-Cookie", self._session_cookie(token, SESSION_TTL))]

    def api_logout(self, user, _body):
        with _db_lock, _db() as c:
            c.execute("DELETE FROM sessions WHERE token=?", (user["session"],))
        return {"ok": True}, [("Set-Cookie", self._session_cookie("", 0))]

    def api_me(self, user, _body):
        return {"username": user["username"], "role": user["role"], "clubId": user["club_id"],
                "clubName": user["club_name"]}

    def api_password(self, user, body):
        _check_new_password(body.get("new"))
        with _db() as c:
            row = c.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
        if not isinstance(body.get("old"), str) or not check_password(row, body["old"]):
            time.sleep(LOGIN_DELAY)
            raise Fail(400, "Huidig wachtwoord klopt niet")
        h, salt = hash_password(body["new"])
        with _db_lock, _db() as c:
            c.execute("UPDATE users SET pass_hash=?, salt=? WHERE id=?", (h, salt, user["id"]))
            c.execute("DELETE FROM sessions WHERE user_id=? AND token<>?", (user["id"], user["session"]))
        return {"ok": True}

    # -- boxes --
    def api_boxes(self, user, _body):
        q = "SELECT b.*, cl.name AS club_name FROM boxes b LEFT JOIN clubs cl ON cl.id=b.club_id"
        with _db() as c:
            if user["role"] == "operator":
                rows = c.execute(q + " ORDER BY cl.name COLLATE NOCASE, b.name COLLATE NOCASE").fetchall()
            else:
                rows = c.execute(q + " WHERE b.club_id=? ORDER BY b.name COLLATE NOCASE", (user["club_id"],)).fetchall()
        return {"boxes": [_box_summary(r) for r in rows]}

    def api_box_detail(self, user, _body, box_id):
        operator = user["role"] == "operator"
        with _db() as c:
            row = _visible_box(c, user, box_id)
            cmds = c.execute("SELECT id, kind, status, ok, result, created, done_at FROM commands "
                             "WHERE box_id=? ORDER BY id DESC LIMIT 30", (box_id,)).fetchall()
        snap, desired, previews = _j(row["snapshot"], {}), _j(row["desired"], {}), _j(row["previews"], {})
        display = dict(desired.get("display") or {})
        # never ship logo data URLs to the browser: only whether one is set + a preview URL
        logos = {k: {"set": bool(display.pop(k, "")), "preview": previews.get(k)} for k in LOGO_FIELDS}
        history = [{"id": r["id"], "kind": r["kind"], "status": r["status"],
                    "ok": None if r["ok"] is None else bool(r["ok"]),
                    "result": None if r["kind"] == "logs" else r["result"],
                    "created": r["created"], "doneAt": r["done_at"]}
                   for r in cmds if operator or r["kind"] not in BOX_COMMANDS]
        out = _box_summary(row)
        out.update(
            display=display, logos=logos, tournaments=desired.get("tournaments") or [], rev=row["rev"],
            login=snap.get("login") or {}, cookieSet=bool(snap.get("cookieSet")), cookieHint=snap.get("cookieHint", ""),
            myTournaments=snap.get("myTournaments") or [], warnings=snap.get("configWarnings") or [],
            commands=history, logsAt=row["logs_at"] if operator else None)
        return out

    def api_box_edit(self, user, body, box_id):
        with _db_lock, _db() as c:
            row = _visible_box(c, user, box_id)
            name = str(body["name"]).strip()[:80] if "name" in body else row["name"]
            club_id = row["club_id"]
            if "clubId" in body:
                club_id = _int(body.get("clubId"))
                if not c.execute("SELECT 1 FROM clubs WHERE id=?", (club_id,)).fetchone():
                    raise Fail(400, "Club bestaat niet")
            c.execute("UPDATE boxes SET name=?, club_id=? WHERE box_id=?", (name, club_id, box_id))
        return {"ok": True}

    def api_link(self, _user, body):
        code, club_id = _norm_code(body.get("code")), _int(body.get("clubId"))
        if len(code) != 6:
            raise Fail(400, "Vul de koppelcode van het scherm in (6 tekens)")
        with _db_lock, _db() as c:
            if not c.execute("SELECT 1 FROM clubs WHERE id=?", (club_id,)).fetchone():
                raise Fail(400, "Kies een club")
            rows = c.execute("SELECT * FROM pending WHERE link_code=? AND last_seen>?",
                             (code, time.time() - LINK_WINDOW)).fetchall()
            if not rows:
                raise Fail(404, "Geen box gevonden met deze code — staat het scherm aan en is het online?")
            if len(rows) > 1:
                raise Fail(409, "Meerdere boxen melden deze code — herstart het scherm voor een nieuwe code")
            p = rows[0]
            if c.execute("SELECT 1 FROM boxes WHERE box_id=?", (p["box_id"],)).fetchone():
                raise Fail(409, "Deze box is al gekoppeld")
            snap = _j(p["snapshot"], {})
            display = snap.get("display") if isinstance(snap.get("display"), dict) else {}
            desired = {"display": display,
                       "tournaments": snap.get("tournaments") if isinstance(snap.get("tournaments"), list) else []}
            name = str(body.get("name") or "").strip()[:80] or str(display.get("clubName") or "Scherm")[:80]
            c.execute("INSERT INTO boxes(box_id, secret_hash, club_id, name, last_seen, snapshot, desired, rev, "
                      "previews, created) VALUES (?,?,?,?,?,?,?,1,'{}',?)",
                      (p["box_id"], p["secret_hash"], club_id, name, p["last_seen"], p["snapshot"],
                       json.dumps(desired), time.time()))
            c.execute("DELETE FROM pending WHERE box_id=?", (p["box_id"],))
        return {"ok": True, "boxId": p["box_id"]}

    def api_unlink(self, user, _body, box_id):
        with _db_lock, _db() as c:
            _visible_box(c, user, box_id)
            c.execute("DELETE FROM commands WHERE box_id=?", (box_id,))
            c.execute("DELETE FROM boxes WHERE box_id=?", (box_id,))
        for k in LOGO_FIELDS:
            _remove_preview(box_id, k)
        return {"ok": True}

    def api_display(self, user, body, box_id):
        d = body.get("display")
        if not isinstance(d, dict) or len(d) > 60:
            raise Fail(400, "Weergave-instellingen ontbreken")
        for k, v in d.items():
            if not re.fullmatch(r"[A-Za-z]{1,40}", k) or not isinstance(v, (str, int, float, bool)):
                raise Fail(400, "Ongeldige waarde voor %s" % k)
            if isinstance(v, str) and len(v) > (MAX_DATA_URL if k in LOGO_FIELDS else 2000):
                raise Fail(413, "Logo te groot (max 5 MB)" if k in LOGO_FIELDS else "Tekst te lang: %s" % k)
        # deep validation (clamping, colours, logo types) happens on the box
        with _db_lock, _db() as c:
            row = _visible_box(c, user, box_id)
            desired, previews = _j(row["desired"], {}), _j(row["previews"], {})
            display = desired.get("display") if isinstance(desired.get("display"), dict) else {}
            for k, v in d.items():
                if k in LOGO_FIELDS:
                    if not isinstance(v, str) or not (v == "" or v.startswith("data:")):
                        continue  # a logo is only changed by a new upload or removal
                    if v:
                        previews[k] = _save_preview(box_id, k, v)
                    else:
                        previews.pop(k, None)
                        _remove_preview(box_id, k)
                display[k] = v
            desired["display"] = display
            c.execute("UPDATE boxes SET desired=?, previews=?, rev=rev+1 WHERE box_id=?",
                      (json.dumps(desired), json.dumps(previews), box_id))
        return {"ok": True}

    def api_tournaments(self, user, body, box_id):
        ts = body.get("tournaments")
        if not isinstance(ts, list) or len(ts) > 50 or not all(isinstance(t, dict) for t in ts):
            raise Fail(400, "Ongeldige toernooienlijst")
        clean = [{"code": str(t.get("code") or "").strip()[:100], "label": str(t.get("label") or "").strip()[:100],
                  "enabled": bool(t.get("enabled", True))} for t in ts]
        with _db_lock, _db() as c:
            row = _visible_box(c, user, box_id)
            desired = _j(row["desired"], {})
            desired["tournaments"] = clean
            c.execute("UPDATE boxes SET desired=?, rev=rev+1 WHERE box_id=?", (json.dumps(desired), box_id))
        return {"ok": True}

    def api_ts_login(self, user, body, box_id):
        if body.get("clear"):
            kind, payload = "clear_login", None
        else:
            u, pw = str(body.get("user") or "").strip(), body.get("password")
            if not u or not isinstance(pw, str) or not pw:
                raise Fail(400, "Vul gebruikersnaam en wachtwoord in")
            kind, payload = "set_login", {"user": u, "pass": pw}
        with _db_lock, _db() as c:
            _visible_box(c, user, box_id)
            return _queue(c, box_id, kind, payload)

    def api_ts_cookie(self, user, body, box_id):
        if body.get("clear"):
            kind, payload = "clear_cookie", None
        else:
            ck = str(body.get("cookie") or "").strip()
            if not ck:
                raise Fail(400, "Vul een cookie in")
            kind, payload = "set_cookie", {"cookie": ck[:8000]}
        with _db_lock, _db() as c:
            _visible_box(c, user, box_id)
            return _queue(c, box_id, kind, payload)

    def api_command(self, user, body, box_id):
        if body.get("kind") not in BOX_COMMANDS:
            raise Fail(400, "Onbekende opdracht")
        with _db_lock, _db() as c:
            _visible_box(c, user, box_id)
            return _queue(c, box_id, body["kind"])

    def api_logs(self, user, _body, box_id):
        with _db() as c:
            row = _visible_box(c, user, box_id)
        return {"logs": row["logs"] or "", "logsAt": row["logs_at"]}

    # -- clubs & users (operator) --
    def api_clubs(self, _user, _body):
        with _db() as c:
            rows = c.execute("SELECT cl.id, cl.name, (SELECT COUNT(*) FROM boxes WHERE club_id=cl.id) AS boxes, "
                             "(SELECT COUNT(*) FROM users WHERE club_id=cl.id) AS users FROM clubs cl "
                             "ORDER BY cl.name COLLATE NOCASE").fetchall()
        return {"clubs": [dict(r) for r in rows]}

    def api_club_create(self, _user, body):
        name = str(body.get("name") or "").strip()[:80]
        if not name:
            raise Fail(400, "Vul een clubnaam in")
        try:
            with _db_lock, _db() as c:
                cid = c.execute("INSERT INTO clubs(name) VALUES (?)", (name,)).lastrowid
        except sqlite3.IntegrityError:
            raise Fail(409, "Deze club bestaat al") from None
        return {"ok": True, "id": cid}

    def api_club_delete(self, _user, _body, club_id):
        with _db_lock, _db() as c:
            used = c.execute("SELECT (SELECT COUNT(*) FROM boxes WHERE club_id=?) + "
                             "(SELECT COUNT(*) FROM users WHERE club_id=?)", (club_id, club_id)).fetchone()[0]
            if used:
                raise Fail(409, "Club heeft nog boxen of gebruikers")
            c.execute("DELETE FROM clubs WHERE id=?", (club_id,))
        return {"ok": True}

    def api_users(self, _user, _body):
        with _db() as c:
            rows = c.execute("SELECT u.id, u.username, u.role, u.club_id AS clubId, cl.name AS clubName FROM users u "
                             "LEFT JOIN clubs cl ON cl.id=u.club_id ORDER BY u.role DESC, u.username").fetchall()
        return {"users": [dict(r) for r in rows]}

    def api_user_create(self, _user, body):
        username, club_id = str(body.get("username") or "").strip(), _int(body.get("clubId"))
        if not re.fullmatch(r"[\w.@+-]{3,64}", username):
            raise Fail(400, "Gebruikersnaam: 3–64 tekens (letters, cijfers, . @ + - _)")
        _check_new_password(body.get("password"))
        h, salt = hash_password(body["password"])
        try:
            with _db_lock, _db() as c:
                if not c.execute("SELECT 1 FROM clubs WHERE id=?", (club_id,)).fetchone():
                    raise Fail(400, "Kies een club")  # a club user always belongs to a club
                uid = c.execute("INSERT INTO users(username, pass_hash, salt, role, club_id) VALUES (?,?,?,'club',?)",
                                (username, h, salt, club_id)).lastrowid
        except sqlite3.IntegrityError:
            raise Fail(409, "Deze gebruikersnaam bestaat al") from None
        return {"ok": True, "id": uid}

    def _club_user(self, c, user_id):
        row = c.execute("SELECT id FROM users WHERE id=? AND role='club'", (user_id,)).fetchone()
        if not row:
            raise Fail(404, "Clubgebruiker niet gevonden")
        return row

    def api_user_delete(self, _user, _body, user_id):
        with _db_lock, _db() as c:
            self._club_user(c, user_id)
            c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM users WHERE id=?", (user_id,))
        return {"ok": True}

    def api_user_password(self, _user, body, user_id):
        _check_new_password(body.get("password"))
        h, salt = hash_password(body["password"])
        with _db_lock, _db() as c:
            self._club_user(c, user_id)
            c.execute("UPDATE users SET pass_hash=?, salt=? WHERE id=?", (h, salt, user_id))
            c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        return {"ok": True}

    # -- demo requests from the public website --
    def api_demo(self, _user, body):
        if str(body.get("website") or "").strip():
            return {"ok": True}  # honeypot filled: a bot — pretend success, store nothing
        _throttle(_client_ip(self))
        name = " ".join(str(body.get("name") or "").split())[:120]
        club = " ".join(str(body.get("club") or "").split())[:120]
        email = str(body.get("email") or "").strip()[:200]
        message = str(body.get("message") or "").strip()[:2000]
        if not name or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise Fail(400, "Vul je naam en een geldig e-mailadres in.")
        lead = {"name": name, "club": club, "email": email, "message": message, "created": time.time()}
        with _db_lock, _db() as c:
            lead_id = c.execute("INSERT INTO leads(name, club, email, message, created) VALUES (?,?,?,?,?)",
                                (name, club, email, message, lead["created"])).lastrowid
        _notify_async(lead_id, lead)
        return {"ok": True}

    def api_leads(self, _user, _body):
        with _db() as c:
            rows = c.execute("SELECT id, name, club, email, message, created, emailed FROM leads "
                             "ORDER BY id DESC LIMIT 500").fetchall()
        return {"leads": [{**dict(r), "emailed": None if r["emailed"] is None else bool(r["emailed"])} for r in rows]}


def main(argv):
    init_db()
    if argv[:1] == ["--create-operator"]:
        if len(argv) != 2:
            print("gebruik: portal.py --create-operator <gebruikersnaam>")
            return 2
        pw = getpass.getpass("Wachtwoord voor %s: " % argv[1])
        if pw != getpass.getpass("Nogmaals: "):
            print("Wachtwoorden komen niet overeen")
            return 1
        try:
            set_operator(argv[1], pw)
        except Fail as e:
            print(e.msg)
            return 1
        print("Beheerder %s opgeslagen" % argv[1])
        return 0
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001
        pass
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print("Toernooi TV portaal op http://%s:%d  (db: %s)" % (HOST, PORT, DB_PATH))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
