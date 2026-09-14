# Toernooi TV

Imported from the Claude Design project and implemented as a runnable local app.

## Files
- `Toernooi TV.dc.html` — management UI (Weergave / Instellingen)
- `Display.dc.html` — the 1920×1080 TV screen (imported by the above)
- `support.js` — the Design Component runtime (unmodified)
- `server.py` — static server **+** `/board` proxy to the TournamentSoftware API (+ portal sync)
- `portal/` — the hosted remote-management portal (see "Portaal")

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

**Install once** on a fresh, supported Raspberry Pi OS (any normal user, any home
directory). Prerequisites: the checkout is owned by that non-root user, lives on a
path **without spaces**, and `origin` is fetchable from the box without
interaction (public repo, or a read-only token baked into the remote URL) —
updates pull from it.

```bash
sudo apt install -y git
git clone https://github.com/woutr-nl/toernooiTV.git toernooi-tv && cd toernooi-tv
# run the newest release (skip when the repo has no v* tags yet)
git checkout "$(git tag -l 'v[0-9]*' --sort=-version:refname | grep -v -- - | head -1)"
sudo bash ./install-kiosk.sh
sudo reboot
# after the reboot:
bash ./verify-appliance.sh
```

The installer derives the app directory from its own location and the service
user from the owner of that directory, and generates the systemd units from the
`appliance/` templates (`__APP__`/`__USER__`/`__UID__` placeholders).

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

## Versie, box-ID & bijwerken

- **Versie** — the tracked `VERSION` file (`dev` when missing). **Box-ID** — a
  uuid generated once on first start into `.boxid` (gitignored), so it survives
  reboots and updates. Both show in **Instellingen → Software** and in `/status`,
  `/health` and `/config`.
- **Update** (only when triggered — never automatic): the **Nu bijwerken** button
  in Instellingen, or `curl -X POST localhost:8770/update`, or directly
  `sudo systemctl start toernooitv-update`. The updater
  (`appliance/update.sh`, run as root by `toernooitv-update.service`) fetches the
  tags from `origin`, checks out the newest `vX.Y.Z` tag, restarts the server,
  waits (60 s) until `/health` reports the new version, then restarts the kiosk.
  A box already on or ahead of the newest release reports `up-to-date`.
- **Rollback** — if the new version doesn't come up healthy, the previous commit is
  checked out again and server + kiosk are restarted (`rolled-back`). If fetching
  fails (offline, remote unreachable) nothing changes (`failed`). Settings, cookie,
  tournaments, logos (`config.json`, `uploads/`) and wifi are never touched.
- **Outcome** — `update-state.json` (`{status, from, to, startedAt, finishedAt, error}`,
  status `running | ok | up-to-date | failed | rolled-back`), shown on the Software
  card and returned as `update` in `/config`. Logs: `journalctl -u toernooitv-update`.

## Portaal (beheer op afstand)

`portal/portal.py` is a small hosted service (Python stdlib + SQLite, no extra
packages) with a Dutch web UI (`portal/portal.html`, vendored React from `vendor/`).
Boxes connect **outward** to it, so they work behind any club wifi/NAT without port
forwarding.

**How a box talks to the portal** — every 10 s `server.py` POSTs a status snapshot
to `{TP_PORTAL_URL}/api/box/sync` (version, wifi SSID + IP, setup-hotspot state,
TournamentSoftware status, effective display settings, tournaments) and gets back
config changes (by revision) and queued commands. Changes reach an online box within
~30 s; commands queued while it is offline run when it reconnects. `config.json`
stays the box's source of truth, so if the portal is unreachable the TV simply keeps
running on its last settings. The TournamentSoftware password and cookie are never
sent to the portal (a new login travels portal → box once and is wiped from the
portal database as soon as the box confirms it).

- `TP_PORTAL_URL` (server env) — default `https://portal.toernooitv.nl`. Only
  `https://` is used (plain `http://` only to `localhost`/`127.0.0.1` for testing);
  set it empty to disable syncing.
- Each box authenticates with its box-ID plus a secret generated on first contact
  (stored in `config.json` → `portal`; the portal keeps only a hash).

**Hosting (Docker Compose)** — the portal runs from the image
`ghcr.io/woutr-nl/toernooitv-portal` (tags: `latest` = newest release, `X.Y.Z` = a
pinned release, `main`/`<sha>` = development builds; see "Een release publiceren").
The same container serves the public website at `/` and the management UI at
**`/portal`**. All configuration lives in the `environment:` block of
`portal/docker-compose.yml` — no `.env` file.

1. **Start** — copy `portal/docker-compose.yml` to the VPS, fill in the `CHANGE-ME`
   placeholders (SMTP settings and the proxy network name), then:
   ```bash
   docker compose pull && docker compose up -d
   ```
   One-time: a new GHCR package is private, so `pull` fails with `denied` until you
   either make it public (GitHub → Packages → `toernooitv-portal` → Package settings →
   Change visibility → **Public**, recommended) or run `docker login ghcr.io` on the
   VPS with a personal access token that has `read:packages`. `:latest` only exists
   after the first `vX.Y.Z` tag pushed after the pipeline was added; until then set
   `image:` to `ghcr.io/woutr-nl/toernooitv-portal:main`. Upgrade = `docker compose
   pull && docker compose up -d` (data lives in volumes).
2. **Operator aanmaken** — against the running deployment:
   ```bash
   docker compose exec portal python3 portal/portal.py --create-operator beheerder
   ```
   It asks for a password; re-run it to reset the password.
3. **Reverse proxy** — the portal publishes no ports. It joins your proxy's existing
   external Docker network (`networks: proxy: name:` in the compose file) and is
   reachable there as `portal:8771`. The proxy must terminate TLS, e.g. a Caddy
   container on the same network:
   ```
   toernooitv.nl, www.toernooitv.nl, portal.toernooitv.nl {
       reverse_proxy portal:8771
   }
   ```
   With nginx, also set `client_max_body_size 20m;` (logo uploads) and pass
   `X-Forwarded-For $proxy_add_x_forwarded_for` (the demo-form throttle uses it).
   `X-Forwarded-For` is only honoured from private-network peers (the proxy). The
   session cookie is `Secure`, so the portal must be served over https
   (`PORTAL_COOKIE_SECURE=0` for a plain-http dev setup only).
4. **Backup** — the volume `portal-data` holds `/data/portal.db` (boxes, accounts and
   demo requests), `portal-uploads` holds `/app/portal/uploads` (logo previews). The
   database uses WAL, so copy it with SQLite's backup API, not a plain file copy:
   ```bash
   docker compose exec portal python3 -c "import sqlite3; sqlite3.connect('/data/portal.db').backup(sqlite3.connect('/data/backup.db'))"
   docker compose cp portal:/data/backup.db ./portal-backup.db
   docker compose exec portal rm /data/backup.db
   docker compose cp portal:/app/portal/uploads ./portal-uploads-backup
   ```
   **Restore** (the files copied in are owned by root, hence the `chown`):
   ```bash
   docker compose cp ./portal-backup.db portal:/data/restore.db
   docker compose exec portal python3 -c "import sqlite3; sqlite3.connect('/data/restore.db').backup(sqlite3.connect('/data/portal.db'))"
   docker compose exec portal rm /data/restore.db
   docker compose cp ./portal-uploads-backup/. portal:/app/portal/uploads/
   docker compose exec -u root portal chown -R portal /app/portal/uploads
   docker compose restart
   ```
   **Moving from an old systemd install** — run `systemctl stop toernooitv-portal` on
   the old host (checkpoints its WAL), then restore its `portal/portal.db` and
   `portal/uploads/` with the restore steps above.

**Website & demo-aanvragen** — `/` is the one-pager (`portal/site.html`, no build step);
its **Inloggen** button opens the portal. The "Plan een demo" form posts to
`POST /api/demo`: the request is stored, a notification mail goes to
`PORTAL_LEAD_TO`, and the visitor sees the thank-you state even when the mail fails.
Requests show up under **Aanvragen** in the portal (operator only, enforced by the
server), with the mail status (*mail verstuurd* / *mail mislukt* / *geen mail
verstuurd* when SMTP isn't configured). Bots are filtered by a hidden honeypot field
and each IP may send at most 3 requests per 15 minutes. Mail settings
(`PORTAL_LEAD_TO`, `PORTAL_LEAD_FROM`, `PORTAL_SMTP_HOST`, `PORTAL_SMTP_PORT`,
`PORTAL_SMTP_USER`, `PORTAL_SMTP_PASS`, `PORTAL_SMTP_STARTTLS`) live in the
`environment:` block of the deployed `docker-compose.yml` — never commit real values.
Change a value and run `docker compose up -d` (recreates the container) to apply it.
`PORTAL_LEAD_TO` defaults to `info@toernooitv.nl`, `PORTAL_LEAD_FROM` to
`PORTAL_SMTP_USER`. Empty `PORTAL_SMTP_HOST` = requests are stored but not mailed.
Port `465` uses SSL; any other port uses STARTTLS unless `PORTAL_SMTP_STARTTLS=0`
(e.g. a local relay).

**Linking a box** — an unlinked box that reaches the portal shows
**Portaal-koppelcode: ABC-123** at the bottom of the TV. The operator clicks
**Box koppelen**, enters that code (valid while the box is online), picks the club and
a name. The portal starts from the box's current settings. **Ontkoppelen** removes the
box from the portal; it shows a new code again and its local `/beheer` is fully
usable again. If a linked box ever loses `config.json` (but keeps `.boxid`) the portal
won't recognise it any more: unlink it and link it again with the code on the TV.

**Roles**
- *Beheerder (operator)* — all boxes, clubs and club users, link/unlink/move boxes,
  display settings, tournaments + TournamentSoftware login, app restart, box reboot,
  software update (with outcome) and logs.
- *Clubgebruiker* — only their own club's boxes: display settings, tournaments and the
  TournamentSoftware login. Everything else is refused by the server, not just hidden.
  The operator creates these accounts; everyone can change their own password.

**On a linked box** the local `/beheer` only offers wifi setup plus a notice that the
box is managed via the portal; `POST /config` and `POST /login` answer 403.

Restart, reboot and logs use the fixed sudo rules in `appliance/sudoers`: boxes
installed before the portal need `sudo bash ./install-kiosk.sh` once after updating
(otherwise those actions report "Niet toegestaan op deze box").

## Een release publiceren

1. Set `VERSION` to `X.Y.Z` and commit.
2. `git tag vX.Y.Z && git push origin main vX.Y.Z` — the tag name must be `v` + the
   `VERSION` content (the updater health-checks the served version against the
   tag's `VERSION`). Tags with a `-` (e.g. `v1.2.0-test`) are never picked.
3. Boxes pick it up on their next triggered update. The same tag publishes the portal
   image `ghcr.io/woutr-nl/toernooitv-portal:X.Y.Z` and moves `:latest` (GitHub
   Actions, `.github/workflows/portal-image.yml`); every push to `main` publishes
   `:main` and `:<sha>`. Tags with a `-` publish only their own tag and never move
   `:latest`. Nothing is pushed if the portal tests fail.

Never move or reuse a tag. If a release changes `appliance/*.service`,
`appliance/sudoers` or `install-kiosk.sh`, re-run `sudo bash ./install-kiosk.sh` on
the box after updating — the updater does not regenerate units or sudoers. Until the
first tag (`v1.0.0`) is pushed, an update ends as `failed` (no release tags).

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
- `/status` — `{ online, ssid, ip, setupMode, hotspotName, version, boxId, managed, linkCode? }`
  (`linkCode` only while the box is not linked to the portal)
- `/config` also returns `managed` and `portalName`; on a portal-managed box
  `POST /config` and `POST /login` return 403
- `/health` — `{ ok, cookieSet, tournaments, version, boxId }`
- `/update` — POST (no body) starts a self-update: `{ ok, message }` or `{ ok:false, error }`
  (already running, or updater not installed); the outcome appears as `update` in `/config`

## Known gaps to resolve with live data
- **Sport split** — the API exposes no tennis/padel field; classification is by
  keyword (`TP_PADEL_KEYWORDS`). Refine once real event/court names are known.
- **Status/result enums** — confirmed `0` = not played, `3` = finished;
  "playing" is derived from `Court.activeMatchID`. Pin down in-progress and the
  `points` set-score structure against a live, scored match.
