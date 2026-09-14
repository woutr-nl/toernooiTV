# Toernooi TV

Imported from the Claude Design project and implemented as a runnable local app.

## Files
- `Toernooi TV.dc.html` — management UI (Weergave / Instellingen)
- `Display.dc.html` — the 1920×1080 TV screen (imported by the above)
- `support.js` — the Design Component runtime (unmodified)
- `server.py` — static server **+** `/board` proxy to the TournamentSoftware API

## Run

```bash
cd ~/toernooi-tv
python3 server.py            # demo data (no cookie)
```

### URLs / routes
- **Fullscreen display (kiosk)** — the bare URL:
  `http://<pi-ip>:8770/`  → e.g. `http://192.168.178.97:8770/`
  Always-on, no chrome, scaled to fit the screen. This is what you cast/show on the TV.
- **Settings (beheer)** — visit deliberately, e.g. from a laptop:
  `http://<pi-ip>:8770/beheer`
  (Both redirect to the underlying `Toernooi%20TV.dc.html` / `#beheer`. The
  "Open weergave →" button in settings returns to the display.)

The `.dc.html` files must be served over HTTP (not opened as `file://`): the
runtime fetches React from a CDN and resolves `<dc-import name="Display">` by
fetching the sibling file. The server binds `0.0.0.0` so the Pi's LAN IP works;
override with `HOST=127.0.0.1` for localhost-only.

## Plug-in-HDMI appliance (kiosk)

Turn the Pi into a device you plug into any TV: on boot it starts the server,
shows the fullscreen display on its own HDMI, and — if it can't find a known
wifi — raises a setup hotspot so you can join the venue's wifi from a phone.

**Install once** (the only step that needs root):

```bash
sudo bash ~/toernooi-tv/install-kiosk.sh
sudo reboot
```

What it sets up:
- `cage` + `chromium` kiosk → fullscreen `http://localhost:8770/` on tty1/HDMI
  (`appliance/toernooitv-kiosk.service`, launcher `appliance/kiosk.sh`)
- `toernooitv-server.service` → the server on boot
- `comitup` → wifi onboarding. No known wifi ⇒ the Pi broadcasts
  **`ToernooiTV-setup-…`**; connect a phone, the captive portal (`http://10.41.0.1`)
  lets you pick the venue's wifi + enter its password. Saved networks auto-join next time.

The UI is fully **self-hosted** — React and fonts are vendored in `vendor/`, so the
screen renders even on captive/blocked networks. (Live match data still needs
internet + a valid cookie; without it the display falls back to demo data.)

**Self-healing & on-screen guidance:**
- On the setup hotspot (or no network) the display shows a fullscreen **"WIFI
  INSTELLEN"** overlay telling staff to join `ToernooiTV-setup-…` from a phone.
- Joined wifi but no internet ⇒ a small "Geen internet" banner; the screen keeps
  rotating. Online but not yet coupled ⇒ a hint pointing at `/beheer`.
- A heartbeat pings the server every 10s and **auto-reloads** the kiosk if it's
  unreachable for ~60s (the server itself restarts via systemd `Restart=always`),
  plus a daily 04:00 refresh. Fed by the `/status` endpoint
  (`{ online, ssid, ip, setupMode, hotspotName }`).

Logs: `journalctl -u toernooitv-server -u toernooitv-kiosk -b`. To stop the kiosk
and get a console back: `sudo systemctl disable --now toernooitv-kiosk; sudo systemctl enable --now getty@tty1`.

## Live match data (cookie-replay, multi-tournament)

The tp-api `/Match` endpoint needs an authenticated **HttpOnly session cookie**
and has no CORS, so the browser can't call it — `server.py` calls it server-side
and exposes a normalized same-origin `/board`. One session cookie covers every
tournament the logged-in account can access.

### Configure from the app (recommended)
1. `python3 server.py`, open the page, go to **Instellingen**.
2. In **Verbinding & toernooien**: paste the session cookie, then add one or more
   tournaments (a name + the tournament code/UUID), and toggle which ones are live.
3. **Opslaan & verbinden.** Enabled tournaments are merged into one rotation,
   split by sport. Per-tournament connection status shows under the list.

To get the cookie: log in to the planner/TV in Chrome → DevTools → Network →
pick a request to `tp-api.tournamentsoftware.com` → copy the full `Cookie:`
request-header value.

The cookie is stored **server-side only** (`config.json`); the UI never receives
it back in full, only a masked status (`…1234`). Tournament list + cookie persist
across restarts in `config.json`.

The UI polls `/board` every 15s; if the cookie is missing, expired (401), or no
tournament has an active/planned match, it falls back to the built-in demo pools
so the screen always renders.

### Optional env seed (first run only, when `config.json` is absent)
| var | default | meaning |
|-----|---------|---------|
| `TP_SESSION_COOKIE` | _(empty)_ | seeds the cookie |
| `TP_TOURNAMENT_CODE` | _(empty)_ | seeds one enabled tournament |
| `TP_PADEL_KEYWORDS` | `padel` | comma-list; event/court name match ⇒ padel, else tennis |
| `TP_LOOKUP_TTL` | `300` | cache seconds for Court/Player/Event |
| `TP_MATCH_TTL` | `5` | cache seconds for Match |
| `PORT` | `8770` | server port |

After first run, configure everything from the Instellingen screen instead.

## Display settings (stored on the box)
Club name/logo, sponsor slide, the **marketing slide** (label, message,
call-to-action, colour), which slide types rotate, seconds per slide type
(banen, sponsor, uitslagen, marketing) and sport colours are stored in
`config.json` under `display` — so a change made from any laptop shows on the TV
within one poll (15s) and survives restarts. Uploaded logos are written to
`uploads/` (gitignored); `config.json` only holds their `/uploads/…` URL.

Browser `localStorage` is no longer authoritative: the first time a browser with
old saved settings opens `/beheer` on a box without stored display settings,
they are carried over to the box once.

## Endpoints
- `/board` — court board merged across enabled tournaments:
  `{ ok, source, courts:[{court,base,num,sport,event,tournamentLabel,t1,t2,status,timeLabel}], counts, tournaments:[{code,label,ok,error,courts}], note }`
- `/config` — GET returns `{ cookieSet, cookieHint, tournaments, login, auth, display }`
  (cookie/password never returned; `display` is `null` until first saved);
  POST `{ cookie?, clearCookie?, tournaments?, display? }` updates + persists.
  `display` may be partial; logos are sent as data-URLs and come back as
  `/uploads/…` URLs (a rejected logo keeps the old one and adds `warning`).
- `/uploads/…` — uploaded logos
- `/health` — `{ ok, cookieSet, tournaments }`

## Known gaps to resolve with live data
- **Sport split** — the API exposes no tennis/padel field; classification is by
  keyword (`TP_PADEL_KEYWORDS`). Refine once real event/court names are known.
- **Status/result enums** — confirmed `0` = not played, `3` = finished;
  "playing" is derived from `Court.activeMatchID`. Pin down in-progress and the
  `points` set-score structure against a live, scored match.
