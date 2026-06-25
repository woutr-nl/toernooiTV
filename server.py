#!/usr/bin/env python3
"""
Toernooi TV — static server + tp-api court-board proxy (multi-tournament).

Serves the .dc.html design files AND:
  GET  /board   normalized court board, merged across all enabled tournaments
  GET  /config  current config (cookie is masked, never returned in full)
  POST /config  update cookie + tournament list (persisted to config.json)
  GET  /health  liveness

The board data is fetched from TournamentSoftware's internal REST API using a
captured, server-side session cookie (the "cookie-replay" approach the official
Toernooi TV uses). Same origin as the page, so the browser never touches tp-api
directly (HttpOnly cookie + no CORS). One session cookie covers every
tournament the logged-in account can access.

Run:
    python3 server.py
Optionally seed the first run from env (used only if config.json is absent):
    TP_SESSION_COOKIE='<cookie>' TP_TOURNAMENT_CODE='<code>' python3 server.py

Open:  http://127.0.0.1:8770/Toernooi%20TV.dc.html
Everything else is configured from the Settings (Instellingen) screen.
"""

import datetime
import http.cookiejar
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

PORT = int(os.environ.get("PORT", "8770"))
HOST = os.environ.get("HOST", "0.0.0.0")  # bind all interfaces so the Pi's LAN IP works
API_BASE = "https://tp-api.tournamentsoftware.com/api/v1/tournament/"
HOTSPOT_PREFIX = os.environ.get("TP_HOTSPOT_PREFIX", "ToernooiTV-setup")
HOTSPOT_IP = os.environ.get("TP_HOTSPOT_IP", "10.41.0.1")  # comitup AP gateway IP
PADEL_KEYWORDS = [
    w.strip().lower()
    for w in os.environ.get("TP_PADEL_KEYWORDS", "padel").split(",")
    if w.strip()
]
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TS_BASE = "https://www.tournamentsoftware.com"
RELOGIN_THROTTLE = int(os.environ.get("TP_RELOGIN_THROTTLE", "60"))  # min sec between auto-relogins
LOOKUP_TTL = int(os.environ.get("TP_LOOKUP_TTL", "300"))
MATCH_TTL = int(os.environ.get("TP_MATCH_TTL", "5"))
SCHEDULE_MAX = int(os.environ.get("TP_SCHEDULE_MAX", "12"))  # max upcoming matches when nothing is live
_NL_DAYS = ["ma", "di", "wo", "do", "vr", "za", "zo"]
_NL_MON = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]
UA = "Mozilla/5.0 (ToernooiTV-proxy)"

_lock = threading.Lock()
_cache = {}  # (code, path) -> (expires_at, data)
CONFIG = {"cookie": "", "tournaments": [], "login": {"user": "", "pass": ""}}
_relogin = {"ts": 0.0}  # throttle auto-relogins

# Live cookie health. The session cookie is an opaque ASP.NET token — its expiry
# is NOT readable from the value, so we can't predict when it dies. Instead we
# observe it: every upstream call records success/failure, and we measure how
# long the current (and last) working streak lasted. Surfaced ONLY via /config
# (the admin screen) — never via /status, so the on-TV display shows no cookie
# errors. okSince = when the current streak began; lastLifetime = how long the
# previous streak survived before a 401 (the best real-world expiry estimate).
AUTH = {"ok": None, "okSince": 0.0, "lastOk": 0.0, "lastFail": 0.0,
        "lastLifetime": 0.0, "error": ""}


def _note_auth(ok, err=""):
    now = time.time()
    with _lock:
        if ok:
            if AUTH["ok"] is not True:
                AUTH["okSince"] = now  # a fresh working streak just started
            AUTH["ok"] = True
            AUTH["lastOk"] = now
            AUTH["error"] = ""
        else:
            if AUTH["ok"] is True and AUTH["okSince"]:
                AUTH["lastLifetime"] = AUTH["lastOk"] - AUTH["okSince"]  # measured expiry
            AUTH["ok"] = False
            AUTH["lastFail"] = now
            AUTH["error"] = err


def _human_dur(sec):
    sec = int(max(0, sec))
    if sec < 60:
        return "%d sec" % sec
    m = sec // 60
    if m < 60:
        return "%d min" % m
    h, m = m // 60, m % 60
    return ("%d u %d min" % (h, m)) if m else ("%d u" % h)


def _auth_status():
    """Derived cookie-health summary for the admin screen."""
    now = time.time()
    if not CONFIG.get("cookie"):
        return {"state": "none", "label": "Niet ingesteld"}
    if AUTH["ok"] is None:
        return {"state": "unknown", "label": "Nog niet getest"}
    if AUTH["ok"]:
        return {
            "state": "ok",
            "label": "Actief · al %s verbonden" % _human_dur(now - AUTH["okSince"]),
            "activeForSec": int(now - AUTH["okSince"]),
            "lastLifetimeSec": int(AUTH["lastLifetime"]) or None,
        }
    extra = ""
    if AUTH["lastLifetime"]:
        extra = " · vorige cookie hield het %s vol" % _human_dur(AUTH["lastLifetime"])
    return {
        "state": "expired",
        "label": "VERLOPEN sinds %s — vernieuw de cookie%s" % (
            _human_dur(now - AUTH["lastFail"]), extra),
        "error": AUTH["error"],
    }


# ---- config load/save -------------------------------------------------------
def _load_config():
    global CONFIG
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            lg = data.get("login") or {}
            CONFIG = {
                "cookie": str(data.get("cookie", "")),
                "tournaments": [_clean_t(t) for t in data.get("tournaments", []) if t.get("code")],
                "login": {"user": str(lg.get("user", "")), "pass": str(lg.get("pass", "")),
                          "name": str(lg.get("name", ""))},
            }
            return
        except Exception as e:  # noqa: BLE001
            print("config.json unreadable, ignoring:", e)
    # seed from env on very first run
    code = os.environ.get("TP_TOURNAMENT_CODE", "").strip()
    CONFIG = {
        "cookie": os.environ.get("TP_SESSION_COOKIE", "").strip(),
        "tournaments": [{"code": code, "label": "Toernooi 1", "enabled": True}] if code else [],
        "login": {"user": os.environ.get("TP_LOGIN_USER", "").strip(),
                  "pass": os.environ.get("TP_LOGIN_PASS", "").strip(), "name": ""},
    }


def _clean_t(t):
    return {
        "code": str(t.get("code", "")).strip(),
        "label": str(t.get("label", "")).strip() or "Toernooi",
        "enabled": bool(t.get("enabled", True)),
    }


def _save_config():
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(CONFIG, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_PATH)


def _masked_config():
    ck = CONFIG.get("cookie", "")
    lg = CONFIG.get("login") or {}
    return {
        "cookieSet": bool(ck),
        "cookieHint": ("…" + ck[-4:]) if len(ck) > 4 else ("set" if ck else ""),
        "tournaments": CONFIG.get("tournaments", []),
        "auth": _auth_status(),
        # credentials are never returned — only the display name + whether stored
        "login": {"user": lg.get("user", ""), "name": lg.get("name", ""),
                  "stored": bool(lg.get("user") and lg.get("pass"))},
    }


# ---- upstream fetch ---------------------------------------------------------
def _fetch(code, path, ttl):
    """GET {API_BASE}{code}{path} as JSON, with the session cookie + TTL cache."""
    now = time.time()
    key = (code, path)
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
        cookie = CONFIG.get("cookie", "")
    req = urllib.request.Request(
        API_BASE + code + path,
        headers={"Cookie": cookie, "User-Agent": UA, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=12) as r:
        data = json.loads(r.read().decode("utf-8"))
    with _lock:
        _cache[key] = (now + ttl, data)
    return data


# ---- headless login ---------------------------------------------------------
# tp.tournamentsoftware.com is a SPA; its login is a plain JSON API call to
# tp-api that sets the .AspNetCore.Cookies session cookie (valid ~7 days). This
# is the SAME API our data fetches use — no cookie wall, anti-forgery, or bot
# protection (unlike the unrelated www.tournamentsoftware.com member login).
TP_LOGIN_URL = "https://tp-api.tournamentsoftware.com/api/v1/User/Login"
TP_LIST_URL = "https://tp-api.tournamentsoftware.com/api/v1/Tournament/List"
TP_ORIGIN = "https://tp.tournamentsoftware.com"
AUTH_COOKIE = ".AspNetCore.Cookies"
_mytourn = {"ts": 0.0, "data": None}  # short cache for the tournament picker


def _login_name(body):
    """Best-effort display name from the Login response JSON; '' if not found."""
    try:
        d = json.loads(body)
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(d, dict):
        return ""
    for k in ("name", "fullName", "displayName", "memberName", "userName"):
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    nm = ("%s %s" % (d.get("firstName") or "", d.get("lastName") or "")).strip()
    return nm


def _cookie_authenticates(cookie_str):
    """Does this cookie actually authenticate against tp-api? Returns True/False,
    or None if we can't tell (no tournament configured / network error). This is
    the definitive success check — far more reliable than scraping login HTML."""
    with _lock:
        tlist = [t for t in CONFIG.get("tournaments", []) if t.get("enabled") and t.get("code")]
    if not tlist:
        return None
    req = urllib.request.Request(
        API_BASE + tlist[0]["code"] + "/Match",
        headers={"Cookie": cookie_str, "User-Agent": UA, "Accept": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=12).read(1)
        return True
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False
        return None
    except Exception:  # noqa: BLE001
        return None


def _ts_login(username, password):
    """Log in via tp-api's JSON endpoint; returns (cookie_str, name, error).

    POST {"Username","Password"} to /api/v1/User/Login. On success the response
    sets the .AspNetCore.Cookies session cookie (valid ~7 days) which we serialise
    for tp-api. 401/400 ⇒ wrong credentials. No cookie wall / anti-forgery here."""
    username, password = (username or "").strip(), (password or "")
    print("login: submitting user=%r passlen=%d" % (username, len(password)))
    if not username or not password:
        return None, "", "gebruikersnaam en wachtwoord vereist"
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    req = urllib.request.Request(
        TP_LOGIN_URL,
        data=json.dumps({"Username": username, "Password": password}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": TP_ORIGIN,
            "Referer": TP_ORIGIN + "/login",
            "User-Agent": BROWSER_UA,
        })
    try:
        resp = opener.open(req, timeout=15)
        out = resp.read().decode("utf-8", "replace")
        code = resp.getcode()
    except urllib.error.HTTPError as e:
        print("login: HTTP %s" % e.code)
        if e.code in (400, 401, 403):
            return None, "", "inloggen mislukt — controleer naam en wachtwoord"
        return None, "", "HTTP %s bij inloggen" % e.code
    except Exception as e:  # noqa: BLE001
        print("login: exception", repr(e))
        return None, "", "verbindingsfout bij inloggen: %s" % e

    cookie_str = "; ".join("%s=%s" % (c.name, c.value) for c in cj)
    names = ",".join(sorted(c.name for c in cj))
    print("login: status=%s cookies=[%s] bodylen=%d" % (code, names, len(out)))
    if not any(c.name == AUTH_COOKIE for c in cj):
        return None, "", "ingelogd maar geen sessiecookie ontvangen"
    name = _login_name(out) or username
    print("login: success, name=%r" % name)
    return cookie_str, name, None


def _try_relogin():
    """Auto re-login with stored credentials when the cookie has expired.
    Throttled so a wrong password can't hammer the login endpoint."""
    now = time.time()
    with _lock:
        lg = CONFIG.get("login") or {}
        user, pwd = lg.get("user", ""), lg.get("pass", "")
        if not (user and pwd) or now - _relogin["ts"] < RELOGIN_THROTTLE:
            return False
        _relogin["ts"] = now
    cookie, name, err = _ts_login(user, pwd)
    if cookie:
        with _lock:
            CONFIG["cookie"] = cookie
            if name:
                CONFIG["login"]["name"] = name
            _cache.clear(); _mytourn["data"]=None
            try:
                _save_config()
            except Exception:  # noqa: BLE001
                pass
        print("auto-relogin OK — cookie refreshed")
        return True
    print("auto-relogin failed:", err)
    return False


def list_my_tournaments():
    """The logged-in account's tournaments, for the admin's add-tournament picker.
    Calls tp-api /Tournament/List with the session cookie; auto-relogins on 401.
    Returns {ok, error, tournaments:[{code,name,startDate,endDate}]} (30s cache)."""
    now = time.time()
    if _mytourn["data"] and now - _mytourn["ts"] < 30:
        return _mytourn["data"]
    with _lock:
        cookie = CONFIG.get("cookie", "")
    if not cookie:
        return {"ok": False, "error": "Niet verbonden — log eerst in", "tournaments": []}

    def _fetch_list():
        with _lock:
            ck = CONFIG.get("cookie", "")
        req = urllib.request.Request(
            TP_LIST_URL, headers={"Cookie": ck, "User-Agent": UA, "Accept": "application/json"})
        return urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "replace")

    try:
        raw = _fetch_list()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403) and _try_relogin():
            try:
                raw = _fetch_list()
            except Exception:  # noqa: BLE001
                return {"ok": False, "error": "Sessie verlopen — log opnieuw in", "tournaments": []}
        else:
            return {"ok": False, "error": "Sessie verlopen — log opnieuw in", "tournaments": []}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "Kon toernooien niet ophalen: %s" % e, "tournaments": []}

    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "Onverwacht antwoord van tp-api", "tournaments": []}
    items = [
        {"code": t.get("code", ""), "name": t.get("name", ""),
         "startDate": t.get("startDate", ""), "endDate": t.get("endDate", "")}
        for t in (data if isinstance(data, list) else [])
        if t.get("code")
    ]
    items.sort(key=lambda t: t.get("startDate") or "", reverse=True)  # newest/upcoming first
    out = {"ok": True, "error": None, "tournaments": items}
    _mytourn.update(ts=now, data=out)
    return out


# ---- normalization ----------------------------------------------------------
def _split_court(name):
    name = (name or "").strip()
    m = re.search(r"(\d+)\s*$", name)
    if m:
        base = name[: m.start()].strip()
        return (base.upper() or "BAAN"), m.group(1)
    return (name.upper() or "BAAN"), ""


def _classify(*texts):
    hay = " ".join(t or "" for t in texts).lower()
    return "padel" if any(kw in hay for kw in PADEL_KEYWORDS) else "tennis"


def _fmt_time(raw):
    if not raw:
        return ""
    m = re.search(r"T(\d{2}):(\d{2})", str(raw))
    if m:
        return m.group(1) + ":" + m.group(2)
    m = re.search(r"(\d{1,2}):(\d{2})", str(raw))
    return (m.group(1).zfill(2) + ":" + m.group(2)) if m else ""


def _fmt_schedule(raw):
    """'do 3 jul 20:00' for a future date, 'ca. HH:MM' for today, '' if unknown."""
    if not raw:
        return ""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})", str(raw))
    if not m:
        return _fmt_time(raw)
    y, mo, d, hh, mm = (int(x) for x in m.groups())
    try:
        dt = datetime.date(y, mo, d)
    except ValueError:
        return "%02d:%02d" % (hh, mm)
    if dt == datetime.date.today():
        return "ca. %02d:%02d" % (hh, mm)
    return "%s %d %s %02d:%02d" % (_NL_DAYS[dt.weekday()], d, _NL_MON[mo - 1], hh, mm)


def _fmt_score(points, home_won):
    """Best-effort 'set-set' score from the API's points list, oriented winner
    first. The exact points shape is unconfirmed (no scored match seen yet), so
    this tries the common shapes and degrades to '' if it can't read them."""
    if not isinstance(points, list) or not points:
        return ""
    sets = []
    for p in points:
        a = b = None
        if isinstance(p, dict):
            for ka, kb in (("home", "away"), ("homeScore", "awayScore"),
                           ("team1", "team2"), ("scoreHome", "scoreAway"),
                           ("homeGames", "awayGames")):
                if ka in p and kb in p:
                    a, b = p[ka], p[kb]
                    break
            if a is None:
                nums = [v for v in p.values() if isinstance(v, int)]
                if len(nums) >= 2:
                    a, b = nums[0], nums[1]
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            a, b = p[0], p[1]
        if isinstance(a, int) and isinstance(b, int):
            sets.append((a, b) if home_won else (b, a))
    return " ".join("%d-%d" % (x, y) for x, y in sets)


def _board_for(code, label):
    """Court tiles for one tournament, plus a per-tournament status row."""
    try:
        matches = _fetch(code, "/Match", MATCH_TTL)
        courts = _fetch(code, "/Court", LOOKUP_TTL)
        players = _fetch(code, "/Player", LOOKUP_TTL)
        events = _fetch(code, "/Event", LOOKUP_TTL)
        _note_auth(True)  # /Match needs the cookie, so a clean fetch means it's valid
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            msg = "401 — cookie verlopen/ongeldig"
            _note_auth(False, msg)  # auth failure → cookie expired/invalid
        else:
            msg = "HTTP %s" % e.code  # bad code / server error — not a cookie problem
        return [], [], {"code": code, "label": label, "ok": False, "error": msg, "courts": 0}
    except Exception as e:  # noqa: BLE001
        return [], [], {"code": code, "label": label, "ok": False, "error": str(e), "courts": 0}

    player_name = {p.get("id"): (p.get("fullName") or p.get("lastName") or "") for p in players}
    event_name = {e.get("eventID", e.get("id")): e.get("name", "") for e in events}
    match_by_id = {m.get("id"): m for m in matches}

    def teams(m):
        home = [player_name.get(p.get("playerID"), "") for p in (m.get("homeMatchPlayers") or [])]
        away = [player_name.get(p.get("playerID"), "") for p in (m.get("awayMatchPlayers") or [])]
        return [x for x in home if x], [x for x in away if x]

    # Court-centric: always emit a tile for every physical court. A court is
    # either PLAYING (a live match is on it) or IDLE — no upcoming/"next" state.
    # Sort by the court NUMBER (the API's displayOrder can run opposite to the
    # numbering), falling back to displayOrder when a name has no number.
    def _court_key(c):
        _, num = _split_court(c.get("name", ""))
        return (0, int(num)) if num.isdigit() else (1, c.get("displayOrder", 0))

    tiles = []
    for c in sorted(courts, key=_court_key):
        cname = c.get("name", "")
        base, num = _split_court(cname)
        active_id = c.get("activeMatchID")
        m, status = None, "idle"
        if active_id and active_id in match_by_id:
            m, status = match_by_id[active_id], "playing"
        sport = _classify(event_name.get(m.get("eventID"), "") if m else "", cname)
        if m:
            home, away = teams(m)
            event = event_name.get(m.get("eventID"), "")
            t1, t2 = " / ".join(home), " / ".join(away)
        else:
            event, t1, t2 = "", "", ""
        tiles.append({
            "court": cname, "base": base, "num": num,
            "sport": sport, "event": event, "tournamentLabel": label,
            "t1": t1, "t2": t2, "status": status,
        })

    # Past results: finished matches with both teams and a decided outcome.
    # result 1 = home won, 2 = away won (per the observed model).
    def _rtime(m):
        return m.get("startDateTime") or m.get("plannedDateTime") or m.get("provisionalPlannedDateTime") or ""
    results = []
    for m in matches:
        home, away = teams(m)
        if not home or not away:
            continue
        # "Played" is signalled by a recorded score (points), not status — a
        # played match can still report status 0. result 1 = home, 2 = away.
        if m.get("result") not in (1, 2):
            continue
        if not m.get("points") and m.get("status") != 3:
            continue
        home_won = m.get("result") == 1
        winner = " / ".join(home if home_won else away)
        loser = " / ".join(away if home_won else home)
        cat = event_name.get(m.get("eventID"), "")  # the category (event) of the match
        pts = m.get("points")
        # A decided match with both teams but no points was won without a played
        # score — a walkover (no-show / withdrawal / reglementair). Flag it so the
        # display shows a "w.o." badge instead of a blank score.
        results.append({
            "sport": _classify(cat),
            "category": cat, "tournamentLabel": label,
            "winner": winner, "loser": loser,
            "score": _fmt_score(pts, home_won),
            "walkover": not pts,
            "_t": _rtime(m), "_id": m.get("id", 0),
        })
    results.sort(key=lambda r: (r["_t"], r["_id"]), reverse=True)  # most recent first
    for r in results:
        r.pop("_t", None); r.pop("_id", None)

    return tiles, results, {"code": code, "label": label, "ok": True, "error": None,
                            "courts": len(tiles),
                            "active": len([t for t in tiles if t["status"] != "idle"]),
                            "results": len(results)}


def build_board():
    with _lock:
        cookie = CONFIG.get("cookie", "")
        tlist = [t for t in CONFIG.get("tournaments", []) if t.get("enabled") and t.get("code")]
    if not cookie:
        return {"ok": False, "source": "no-cookie",
                "error": "Sessiecookie niet ingesteld", "courts": [], "tournaments": []}
    if not tlist:
        return {"ok": False, "source": "no-tournaments",
                "error": "Geen toernooien ingeschakeld", "courts": [], "tournaments": []}

    def _gather():
        tiles_, results_, statuses_ = [], [], []
        for t in tlist:
            ti, re_, st = _board_for(t["code"], t["label"])
            tiles_.extend(ti)
            results_.extend(re_)
            statuses_.append(st)
        return tiles_, results_, statuses_

    all_tiles, all_results, statuses = _gather()
    # Cookie expired but credentials are stored → refresh it and try once more,
    # so an unattended kiosk self-heals instead of falling back to empty.
    auth_failed = any((s.get("error") or "").startswith("401") for s in statuses)
    if not all_tiles and auth_failed and _try_relogin():
        all_tiles, all_results, statuses = _gather()

    counts = {"tennis": sum(x["sport"] == "tennis" for x in all_tiles),
              "padel": sum(x["sport"] == "padel" for x in all_tiles)}
    any_ok = any(s["ok"] for s in statuses)
    return {
        "ok": bool(all_tiles), "source": "live",
        "courts": all_tiles, "results": all_results, "counts": counts,
        "tournaments": statuses, "note": None,
        "error": None if any_ok else "Geen enkel toernooi gaf data terug",
    }


# ---- device/network status --------------------------------------------------
_status_cache = {"ts": 0.0, "data": None}


def _is_online():
    """Cheap internet reachability probe (DNS port on a public resolver)."""
    for host in ("1.1.1.1", "8.8.8.8"):
        try:
            s = socket.create_connection((host, 53), timeout=2)
            s.close()
            return True
        except OSError:
            continue
    return False


def _wifi_ssid():
    """SSID of the active wifi connection, '' if none/unknown."""
    try:
        out = subprocess.run(
            ["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"],
            capture_output=True, text=True, timeout=3,
        ).stdout
        for line in out.splitlines():
            if line.startswith("yes:"):
                return line.split(":", 1)[1]
    except Exception:  # noqa: BLE001 — nmcli missing/slow ⇒ unknown
        pass
    return ""


def _lan_ip():
    """Best-effort primary LAN IP."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.255.255", 1))  # no packet sent; picks egress iface
        return s.getsockname()[0]
    except Exception:  # noqa: BLE001
        return "127.0.0.1"
    finally:
        s.close()


# ---- wifi via comitup's D-Bus API ------------------------------------------
# comitup runs as root and owns the wifi state machine (AP ⇄ client). Its D-Bus
# policy allows context="default", so this server (running as a normal user) may
# call it directly — no sudo/polkit. We drive it for BOTH the on-device setup
# portal (served on :80 while the hotspot is up) and the /beheer wifi card, so a
# single branded wifi picker replaces comitup-web. Methods: state()->('MODE',
# 'name'), access_points()->[{ssid,strength,security}], connect(ssid,pw),
# delete_connection(), get_info()->{apname,…}.
_COMITUP_BUS = "com.github.davesteele.comitup"
_COMITUP_OBJ = "/com/github/davesteele/comitup"
_wifi_lock = threading.Lock()
_scan_cache = {"ts": 0.0, "nets": None}


def _comitup():
    import dbus  # lazy import: absent on dev machines, present on the appliance
    bus = dbus.SystemBus()
    return dbus.Interface(bus.get_object(_COMITUP_BUS, _COMITUP_OBJ), _COMITUP_BUS)


def comitup_state():
    """(mode, name) e.g. ('CONNECTED','Vinknet5') or ('HOTSPOT','…'); ('','') if comitup is absent."""
    try:
        m, s = _comitup().state()
        return str(m), str(s)
    except Exception:  # noqa: BLE001 — comitup/dbus missing ⇒ caller falls back to nmcli
        return "", ""


def comitup_info():
    try:
        return {str(k): str(v) for k, v in _comitup().get_info().items()}
    except Exception:  # noqa: BLE001
        return {}


def comitup_scan(max_age=10):
    """Visible networks: [{ssid, strength(0-100), secured}], strongest first, deduped. Cached briefly."""
    now = time.time()
    with _wifi_lock:
        if _scan_cache["nets"] is not None and now - _scan_cache["ts"] < max_age:
            return _scan_cache["nets"]
    nets = {}
    try:
        for ap in _comitup().access_points():
            ssid = str(ap.get("ssid", "")).strip()
            if not ssid:
                continue
            try:
                strength = int(float(ap.get("strength", 0)))
            except (TypeError, ValueError):
                strength = 0
            sec = str(ap.get("security", "")).strip().lower()
            secured = sec not in ("", "none", "unencrypted", "open")
            prev = nets.get(ssid)
            if prev is None or strength > prev["strength"]:
                nets[ssid] = {"ssid": ssid, "strength": strength, "secured": secured}
    except Exception:  # noqa: BLE001
        pass
    out = sorted(nets.values(), key=lambda n: -n["strength"])
    with _wifi_lock:
        _scan_cache.update(ts=now, nets=out)
    return out


def comitup_connect(ssid, password):
    """Ask comitup to join `ssid`. Async on comitup's side (it tears down the AP and
    joins, so an attached phone briefly drops). Raises on a D-Bus error."""
    _comitup().connect(str(ssid), str(password or ""))


def comitup_forget():
    """Drop the current wifi connection — comitup re-raises its setup hotspot."""
    _comitup().delete_connection()


def device_status():
    """Online / wifi / setup-hotspot state for the on-screen overlay (cached)."""
    now = time.time()
    if _status_cache["data"] and now - _status_cache["ts"] < 8:
        return _status_cache["data"]
    online = _is_online()
    ip = _lan_ip()
    mode, cname = comitup_state()  # authoritative when comitup is present
    if mode:
        on_hotspot = mode == "HOTSPOT"
        ssid = "" if on_hotspot else cname
        setup = on_hotspot or (mode == "CONNECTING" and not online)
        hotspot_name = comitup_info().get("apname", "") if on_hotspot else ""
    else:  # comitup absent (e.g. dev box) — fall back to nmcli read-only probe
        ssid = _wifi_ssid()
        on_hotspot = ssid.startswith(HOTSPOT_PREFIX)
        setup = on_hotspot or (not ssid and not online)
        hotspot_name = ssid if on_hotspot else ""
    data = {
        "online": online,
        "ssid": ssid,
        "ip": ip,
        "mode": mode,
        # show the wifi-setup overlay when sitting on our own setup AP, or when
        # there's no wifi joined and no internet at all
        "setupMode": setup,
        "hotspotName": hotspot_name or (HOTSPOT_PREFIX + "-…"),
    }
    _status_cache.update(ts=now, data=data)
    return data


# ---- http handler -----------------------------------------------------------
class Handler(SimpleHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length", "0") or "0")
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def end_headers(self):
        # Never let a browser cache the app shell — on a kiosk/appliance there's
        # no easy cache-bust, so a stale .dc.html/JS would hide UI changes after
        # an update. (JSON endpoints already set their own Cache-Control.)
        p = self.path.split("?")[0].lower()
        if p.endswith((".dc.html", ".js", ".css")) and "cache-control" not in self._headers_buffer_keys():
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _headers_buffer_keys(self):
        # names of headers already queued in this response (lowercased)
        keys = []
        for raw in getattr(self, "_headers_buffer", []) or []:
            try:
                line = raw.decode("latin-1")
            except Exception:  # noqa: BLE001
                continue
            if ":" in line:
                keys.append(line.split(":", 1)[0].strip().lower())
        return keys

    def do_GET(self):
        path = self.path.split("?")[0]
        # tidy routes: serve the app in-place (URL stays clean, no filename shown).
        # bare URL = fullscreen display, /beheer = settings (client reads the path).
        if path in ("/", "/weergave", "/display", "/beheer", "/instellingen",
                    "/settings", "/setup", "/wifi-setup"):
            self.path = "/Toernooi%20TV.dc.html"
            return super().do_GET()
        if path == "/board":
            try:
                self._json(build_board())
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "source": "error", "error": str(e),
                            "courts": [], "tournaments": []}, 500)
            return
        if path == "/config":
            with _lock:
                self._json(_masked_config())
            return
        if path == "/status":
            try:
                self._json(device_status())
            except Exception as e:  # noqa: BLE001
                self._json({"online": True, "setupMode": False, "error": str(e)})
            return
        if path == "/wifi":
            try:
                st = device_status()
                self._json({
                    "ok": True,
                    "mode": st.get("mode", ""),
                    "ssid": st.get("ssid", ""),
                    "ip": st.get("ip", ""),
                    "online": st.get("online", False),
                    "setupMode": st.get("setupMode", False),
                    "networks": comitup_scan(),
                })
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "error": str(e), "networks": []})
            return
        if path == "/health":
            self._json({"ok": True, "cookieSet": bool(CONFIG.get("cookie")),
                        "tournaments": len(CONFIG.get("tournaments", []))})
            return
        if path == "/my-tournaments":
            try:
                self._json(list_my_tournaments())
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "error": str(e), "tournaments": []})
            return
        # On the setup hotspot, push stray navigations / captive-portal probes to
        # our wifi page so the phone shows a working portal (replaces comitup-web).
        if self._maybe_captive():
            return
        super().do_GET()

    def _maybe_captive(self):
        """While the setup hotspot is up, 302 unknown paths (OS captive-portal
        probes, random navigations) to our wifi setup page. Real local files
        (the page, vendored React/fonts) still serve normally so the portal
        actually renders."""
        try:
            if device_status().get("mode") != "HOTSPOT":
                return False
        except Exception:  # noqa: BLE001
            return False
        if os.path.isfile(self.translate_path(self.path)):
            return False
        self._redirect("http://%s/setup" % HOTSPOT_IP)
        return True

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/config", "/login", "/wifi"):
            self._json({"ok": False, "error": "unknown endpoint"}, 404)
            return
        try:
            body = self._read_json()
        except Exception as e:  # noqa: BLE001
            self._json({"ok": False, "error": "bad JSON: %s" % e}, 400)
            return

        if path == "/wifi":
            # Set wifi by hand: forget the current network, or join a chosen one.
            # comitup does the work as root; the join is async and tears down the
            # setup AP, so a phone attached to it will drop mid-connect (expected).
            if body.get("forget"):
                try:
                    comitup_forget()
                except Exception as e:  # noqa: BLE001
                    self._json({"ok": False, "error": "vergeten mislukt: %s" % e}, 500)
                    return
                _status_cache["data"] = None
                self._json({"ok": True, "message": "Wifi vergeten — het instelnetwerk komt weer op."})
                return
            ssid = str(body.get("ssid", "")).strip()
            if not ssid:
                self._json({"ok": False, "error": "Geen netwerk gekozen."}, 400)
                return
            try:
                comitup_connect(ssid, body.get("password", ""))
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "error": "verbinden mislukt: %s" % e}, 500)
                return
            _status_cache["data"] = None  # force a fresh status on the next poll
            self._json({"ok": True, "message": "Bezig met verbinden met ‘%s’…" % ssid})
            return

        if path == "/login":
            # Log in to tournamentsoftware.com and capture the cookie for the user.
            # store=True (default) keeps the credentials so the cookie auto-renews
            # when it expires; store=False logs in once without saving them.
            cookie, name, err = _ts_login(body.get("user", ""), body.get("password", ""))
            if err:
                self._json({"ok": False, "error": err}, 200)
                return
            with _lock:
                CONFIG["cookie"] = cookie
                if body.get("store", True):
                    CONFIG["login"] = {"user": str(body.get("user", "")).strip(),
                                       "pass": str(body.get("password", "")), "name": name}
                else:  # one-shot login — remember the name to show, but not the password
                    CONFIG["login"] = {"user": str(body.get("user", "")).strip(),
                                       "pass": "", "name": name}
                _cache.clear(); _mytourn["data"]=None
                AUTH.update(ok=None, error="")  # let the next /board poll confirm validity
                try:
                    _save_config()
                except Exception as e:  # noqa: BLE001
                    self._json({"ok": False, "error": "kon config niet opslaan: %s" % e}, 500)
                    return
                out = _masked_config()
            out.update(ok=True, message=("Ingelogd als %s" % name) if name else "Ingelogd ✓")
            self._json(out)
            return

        with _lock:
            if "tournaments" in body and isinstance(body["tournaments"], list):
                CONFIG["tournaments"] = [_clean_t(t) for t in body["tournaments"] if t.get("code")]
            if body.get("clearCookie"):
                CONFIG["cookie"] = ""
            elif body.get("cookie"):  # only overwrite when a non-empty value is sent
                CONFIG["cookie"] = str(body["cookie"]).strip()
            if body.get("clearLogin"):
                CONFIG["login"] = {"user": "", "pass": ""}  # stop auto-renew, forget password
            _cache.clear(); _mytourn["data"]=None  # config changed → drop cached data
            try:
                _save_config()
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "error": "kon config niet opslaan: %s" % e}, 500)
                return
            out = _masked_config()
        out["ok"] = True
        self._json(out)

    def log_message(self, fmt, *args):
        line = args[0] if args else ""
        if "/board" in line or "/config" in line:
            return
        super().log_message(fmt, *args)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(line_buffering=True)  # so diagnostics reach the journal promptly
    except Exception:  # noqa: BLE001
        pass
    os.chdir(HERE)
    _load_config()
    ck = CONFIG.get("cookie", "")
    ip = _lan_ip()
    print("Toernooi TV server bound to %s:%d" % (HOST, PORT))
    print("  cookie    :", ("set (%d chars)" % len(ck)) if ck else "NOT set → demo fallback")
    print("  tournaments:", len(CONFIG.get("tournaments", [])))
    print("  on this Pi : http://127.0.0.1:%d/Toernooi%%20TV.dc.html" % PORT)
    if ip != "127.0.0.1":
        print("  on the LAN : http://%s:%d/Toernooi%%20TV.dc.html" % (ip, PORT))
    print("  configure  : Instellingen screen, or edit config.json")

    primary = ThreadingHTTPServer((HOST, PORT), Handler)
    # Optional second listener on a privileged port (usually 80) so the box is
    # reachable at toernooitv.local/beheer with no :port. Best-effort: if 80 is
    # taken (the comitup setup portal owns it while the hotspot is up) or binding
    # isn't permitted, we just skip it — the primary listener always runs, and
    # the kiosk always talks to PORT (8770), so nothing breaks.
    extra = int(os.environ.get("EXTRA_PORT", "0") or "0")
    if extra and extra != PORT:
        try:
            s2 = ThreadingHTTPServer((HOST, extra), Handler)
            threading.Thread(target=s2.serve_forever, daemon=True).start()
            tail = "" if extra == 80 else (":%d" % extra)
            print("  port-free  : http://%s%s/  (also toernooitv.local%s/beheer)" % (ip, tail, tail))
        except OSError as e:
            print("  extra port %d unavailable (%s) — serving on %d only" % (extra, e, PORT))

    try:
        primary.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
