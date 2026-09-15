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

**Install once** on a fresh, supported Raspberry Pi OS, from a normal (non-root)
user's shell:

```bash
curl -fsSL https://raw.githubusercontent.com/woutr-nl/toernooiTV/main/install.sh | sudo bash
```

It installs git, clones the repo as your user into `~/toernooi-tv`, checks out the
newest release (`v*` tag; it stays on `main` when none exist yet), runs
`install-kiosk.sh` and prints the next steps (reboot, `verify-appliance.sh`). It is
safe to re-run: an existing checkout is fetched instead of cloned. Optional
overrides: `TOERNOOITV_USER` (the box's user instead of the one running sudo),
`TOERNOOITV_DIR` (checkout path, without spaces) and `TOERNOOITV_REBOOT=1` (reboot
at the end), e.g.
`curl -fsSL https://raw.githubusercontent.com/woutr-nl/toernooiTV/main/install.sh | sudo TOERNOOITV_REBOOT=1 bash`.

**Manual alternative** (any normal user, any home directory). Prerequisites: the
checkout is owned by that non-root user, lives on a path **without spaces**, and
`origin` is fetchable from the box without interaction (public repo, or a read-only
token baked into the remote URL) — updates pull from it.

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
  (`appliance/toernooitv-kiosk.service`, launcher `appliance/kiosk.sh`). The TV
  shows no mouse pointer: cage draws its `left_ptr` arrow at screen centre, so
  `kiosk.sh` links a transparent cursor theme (`appliance/hidden-cursor-theme`)
  as `~/.icons/default` itself — updates via "Nu bijwerken" need no reinstall. A
  plugged-in mouse shows its pointer while moving; the display page hides it
  again ~4 s after the last movement.
- `toernooitv-server.service` → the server on boot
- `comitup` → wifi onboarding. No known wifi ⇒ the Pi broadcasts
  **`ToernooiTV-setup-…`**; connect a phone, the captive portal (`http://10.41.0.1`)
  lets you pick the venue's wifi + enter its password. Saved networks auto-join next time.
  comitup's own web portal (`comitup-web`) is masked: our server serves a simple,
  phone-sized wifi page at `/setup` (every unknown URL redirects there while the
  hotspot is up) and drives comitup over D-Bus. `/beheer` keeps the full dashboard,
  including its wifi card. `GET /wifi` also reports `radio` (`ok`/`blocked`/`unavailable`)
  and `scanError`, so both pages explain an empty network list.
- Wifi country **NL** (`raspi-config nonint do_wifi_country NL`, or `iw reg set NL` +
  `cfg80211 ieee80211_regdom=NL`) and `rfkill unblock wifi` — without a country a
  fresh Pi OS keeps the radio soft-blocked and no hotspot is broadcast.

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
- **Hotspot DHCP watchdog** (`toernooitv-hotspot-dhcp.timer`, every 30 s, as root,
  script `appliance/hotspot-dhcp.sh`). comitup 1.43 starts the hotspot's dnsmasq from a
  NetworkManager callback; in the field that callback was sometimes missed, so the
  hotspot was visible but phones got no IP. The root cause is unconfirmed, so instead of
  patching comitup we enforce the invariant: wlan0 on `ToernooiTV-setup…` and no
  dnsmasq with comitup's `dns-hotspot.conf` ⇒ start one (in its own transient unit,
  `toernooitv-hotspot-dnsmasq`). It uses comitup's conf and pid-file, so comitup still
  stops it when it joins a network. comitup's own unit already starts after
  NetworkManager, so no ordering drop-in is added. `verbose: 1` in
  `appliance/comitup.conf` writes diagnostics to `/var/log/comitup.log`
  ("Running dnsmasq", "nmm - primary state"); watchdog actions log to
  `journalctl -t toernooitv-hotspot-dhcp`.

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
  A box already on or ahead of the newest release reports `up-to-date`. If `origin`
  has no release tags yet, nothing changes and the box reports `no-release`, shown in
  neutral grey as "Nog geen release beschikbaar" (not a failure).
- **Rollback** — if the new version doesn't come up healthy, the previous commit is
  checked out again and server + kiosk are restarted (`rolled-back`). If fetching
  fails (offline, remote unreachable) or the checkout fails, nothing changes
  (`failed`). Settings, cookie,
  tournaments, logos (`config.json`, `uploads/`) and wifi are never touched.
- **Outcome** — `update-state.json` (`{status, from, to, startedAt, finishedAt, error}`,
  status `running | ok | up-to-date | no-release | failed | rolled-back`), shown on the Software
  card and returned as `update` in `/config`. Logs: `journalctl -u toernooitv-update`.

## Portaal (beheer op afstand)

`portal/portal.py` is a small hosted service (Python + PostgreSQL (psycopg), see
`portal/requirements.txt`) with a Dutch web UI (`portal/portal.html`, vendored React from `vendor/`).
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

- `TP_PORTAL_URL` (server env) — default `https://toernooitv.nl`. Only
  `https://` is used (plain `http://` only to `localhost`/`127.0.0.1` for testing);
  set it empty to disable syncing. Boxes that got a manual workaround drop-in
  (`/etc/systemd/system/toernooitv-server.service.d/*.conf` setting
  `TP_PORTAL_URL=https://toernooitv.nl`) no longer need it: remove the file, then
  `sudo systemctl daemon-reload && sudo systemctl restart toernooitv-server`.
- Sync failures (TLS, DNS, timeouts, HTTP errors) are logged to
  `journalctl -u toernooitv-server` (first failure, on a changed reason, then at most
  every 10 minutes, plus one line on recovery) and reported as `portal` in `/status`.
  `verify-appliance.sh` also checks that the portal answers.
- Each box authenticates with its box-ID plus a secret generated on first contact
  (stored in `config.json` → `portal`; the portal keeps only a hash).

**Hosting (Docker Compose)** — the portal runs from the image
`ghcr.io/woutr-nl/toernooitv-portal` (tags: `latest` = newest release, `X.Y.Z` = a
pinned release, `main`/`<sha>` = development builds; see "Een release publiceren").
The same container serves the public website at `/` and the management UI at
**`/portal`**. All configuration lives in the `environment:` block of
`portal/docker-compose.yml` — no `.env` file. The compose file also runs the
database: a `db` service (`postgres:16-alpine`) with its data in the `portal-pgdata`
volume. It publishes no ports and is not on the proxy network; only the portal reaches
it, over the compose project's internal network.

1. **Start** — copy `portal/docker-compose.yml` to the VPS, fill in the `CHANGE-ME`
   placeholders (database password, SMTP settings and the proxy network name), then:
   ```bash
   docker compose pull && docker compose up -d
   ```
   The database password goes in **two** places that must match: `POSTGRES_PASSWORD`
   (service `db`) and inside `PORTAL_DATABASE_URL`
   (`postgresql://portal:<password>@db:5432/portal`, service `portal`). URL-encode
   special characters in the URL (e.g. `@` → `%40`), or pick a password without them.
   `POSTGRES_PASSWORD` only takes effect when the `portal-pgdata` volume is first
   created; to change it later run
   `docker compose exec db psql -U portal -d portal -c "ALTER USER portal PASSWORD 'new'"`,
   then update both values and `docker compose up -d`. `db` must be healthy before the
   portal starts (`depends_on`), and the portal retries the connection for 30 s on boot.
   One-time: a new GHCR package is private, so `pull` fails with `denied` until you
   either make it public (GitHub → Packages → `toernooitv-portal` → Package settings →
   Change visibility → **Public**, recommended) or run `docker login ghcr.io` on the
   VPS with a personal access token that has `read:packages`. `:latest` exists
   once the pipeline has released a `vX.Y.Z` (the first green run on `main` tags the
   current `VERSION`); before that, `image:` can point at
   `ghcr.io/woutr-nl/toernooitv-portal:main`. Upgrade = `docker compose
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
   Boxes sync to `toernooitv.nl` by default; `portal.toernooitv.nl` remains the name
   for the management UI once its certificate is in place.
   With nginx, also set `client_max_body_size 20m;` (logo uploads) and pass
   `X-Forwarded-For $proxy_add_x_forwarded_for` (the demo-form throttle uses it).
   `X-Forwarded-For` is only honoured from private-network peers (the proxy). The
   session cookie is `Secure`, so the portal must be served over https
   (`PORTAL_COOKIE_SECURE=0` for a plain-http dev setup only).
4. **Backup** — the volume `portal-pgdata` holds the PostgreSQL database (boxes,
   accounts and demo requests), `portal-uploads` holds `/app/portal/uploads` (logo
   previews). Dump the database with `pg_dump` (consistent while the portal runs):
   ```bash
   docker compose exec -T db pg_dump -U portal -Fc portal > portal.dump
   docker compose cp portal:/app/portal/uploads ./portal-uploads-backup
   ```
   **Restore** — stop the portal first so no request writes during the restore (the
   uploads copied in are owned by root, hence the `chown`, which needs the container
   running):
   ```bash
   docker compose stop portal
   docker compose exec -T db pg_restore -U portal -d portal --clean --if-exists < portal.dump
   docker compose start portal
   docker compose cp ./portal-uploads-backup/. portal:/app/portal/uploads/
   docker compose exec -u root portal chown -R portal /app/portal/uploads
   docker compose restart portal
   ```

**Local development & tests** — start a throwaway PostgreSQL, install the driver, and
point the portal (`PORTAL_DATABASE_URL`) or the test suite (`PORTAL_TEST_DATABASE_URL`)
at it. The tests wipe that database's `public` schema, so never aim them at real data:
```bash
docker run --rm -e POSTGRES_PASSWORD=portal -e POSTGRES_USER=portal -e POSTGRES_DB=portal -p 5432:5432 postgres:16-alpine
pip install -r portal/requirements.txt
PORTAL_DATABASE_URL=postgresql://portal:portal@localhost:5432/portal PORTAL_COOKIE_SECURE=0 python3 portal/portal.py
PORTAL_TEST_DATABASE_URL=postgresql://portal:portal@localhost:5432/portal python3 test_portal.py
```

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

The hero of the one-pager embeds a live TV preview from `/demo`: `portal/demo.html`
drives the real `Display.dc.html` through `support.js` with fixed demo data (no box,
login or tournament data), rotating between courts, results, sponsor and marketing
screens. Both `Display.dc.html` and `support.js` are copied into the portal image for
this, and the portal also serves them at `/Display.dc.html` and `/support.js`.
`/og.png` (`portal/og.png`) is the social-sharing preview image.

**Linking a box** — an unlinked box that reaches the portal shows
**Portaal-koppelcode: ABC-123** at the bottom of the TV (if the box can't reach the
portal, the TV shows `Portaal niet bereikbaar — <reden>` instead of the code). The operator clicks
**Box koppelen**, enters that code (valid while the box is online), picks the club and
a name. The portal starts from the box's current settings. **Ontkoppelen** removes the
box from the portal; it shows a new code again and its local `/beheer` is fully
usable again. If a linked box ever loses `config.json` (but keeps `.boxid`) the portal
won't recognise it any more: unlink it and link it again with the code on the TV.

**Roles**
- *Beheerder (operator)* — all boxes, clubs and club users, link/unlink/move boxes,
  display settings, tournaments + TournamentSoftware login, app restart, box reboot,
  software update (with outcome) and logs. Via **Bekijk als club** (on Clubs & gebruikers,
  or a club header on Schermen) the operator uses the portal exactly as that club's admin,
  also for a club without club users; no club account or password is needed. A banner
  "Je bekijkt als …" with **Terug naar beheer** stays visible meanwhile, and the server
  enforces the club-level rights during it.
- *Clubgebruiker* — only their own club's boxes: display settings, tournaments and the
  TournamentSoftware login. Everything else is refused by the server, not just hidden.
  The operator creates these accounts; everyone can change their own password.

**On a linked box** the local `/beheer` only offers wifi setup plus a notice that the
box is managed via the portal; `POST /config` and `POST /login` answer 403.

Restart, reboot and logs use the fixed sudo rules in `appliance/sudoers`: boxes
installed before the portal need `sudo bash ./install-kiosk.sh` once after updating
(otherwise those actions report "Niet toegestaan op deze box").

## Een release publiceren

1. Set `VERSION` to `X.Y.Z`, commit and push to `main`.
2. GitHub Actions (`.github/workflows/portal-image.yml`) does the rest: every push to
   `main` runs the portal tests and publishes `:main` and `:<sha>`; then the `release`
   job checks `VERSION`. If it is a plain `X.Y.Z` and tag `vX.Y.Z` doesn't exist yet,
   it retags that image as `ghcr.io/woutr-nl/toernooitv-portal:X.Y.Z` + `:latest` and
   creates tag `vX.Y.Z` on the commit plus a GitHub Release. The rule is "untagged
   plain `X.Y.Z` on `main`", so it also fires on pushes that don't touch `VERSION`.
   An existing tag, or a `VERSION` like `dev`/`1.2.0-test`/missing, is skipped without
   failing the run. Nothing is tagged or pushed if the portal tests fail.
3. Boxes pick it up on their next triggered update.

The tag name is always `v` + the `VERSION` content (the updater health-checks the
served version against the tag's `VERSION`). Tagging by hand still works:
`git tag vX.Y.Z && git push origin vX.Y.Z` publishes `:X.Y.Z` and moves `:latest`.
Tags with a `-` (e.g. `v1.2.0-test`) are never picked by boxes or `install.sh`, publish
only their own image tag and never move `:latest`.

Never move or reuse a tag. If a release changes `appliance/*.service`,
`appliance/sudoers` or `install-kiosk.sh`, re-run `sudo bash ./install-kiosk.sh` on
the box after updating — the updater does not regenerate units or sudoers.

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
- `/status` — `{ online, ssid, ip, setupMode, hotspotName, version, boxId, managed, linkCode?, portal? }`
  (`linkCode` only while the box is not linked to the portal)
  `portal = { ok, lastOk (epoch s or null), error (Dutch reason or null) }`, present whenever
  portal sync is enabled; the TV only shows `linkCode` as usable while `portal.ok` is true.
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
