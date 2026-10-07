# Running a party from the Mac

Guests join from anywhere (cellular included) through a Cloudflare Tunnel to
the jukebox on your Mac. The Mac has to be at the party anyway: it is the
Spotify playback device and party-lights listens to its audio. So the jukebox
lives there too, and the tunnel is just a stable public HTTPS address in front
of it.

```
phone ──https──▶ party.example.com ──Cloudflare──▶ cloudflared (Mac) ──▶ jukebox 127.0.0.1:5001
                                                                     party-lights ──▶ localhost:5001/api/now
```

## One-time setup

1. **A domain on Cloudflare.** Any domain whose DNS is on Cloudflare (a cheap
   one is fine). You'll use a subdomain like `party.example.com`.
2. **cloudflared:**
   ```sh
   brew install cloudflared
   cloudflared tunnel login                 # pick your domain in the browser
   cloudflared tunnel create party-jukebox  # prints the tunnel UUID
   cloudflared tunnel route dns party-jukebox party.example.com
   cp deploy/cloudflared/config.yml.example deploy/cloudflared/config.yml
   ```
   Fill in the UUID, the credentials path it printed, and your hostname.
   `config.yml` is gitignored.
3. **Spotify dashboard:** add `https://party.example.com/auth/callback` as a
   Redirect URI on your app.
4. **`.env`:**
   ```sh
   SPOTIFY_REDIRECT_URI=https://party.example.com/auth/callback
   PARTY_URL=https://party.example.com
   PORT=5001                   # not 5000: AirPlay Receiver owns it
   HOST_PASSWORD=...           # 8+ characters, not the default
   FLASK_SECRET_KEY=...        # python -c "import secrets; print(secrets.token_hex(32))"
   ```
   The app refuses to start while the secret key is short or the password is
   a default.
5. **Install the services** (start at login, restart if they crash, keep the
   Mac awake while the jukebox runs):
   ```sh
   deploy/install.sh                # jukebox + tunnel
   deploy/install.sh --with-lights  # also party-lights
   ```
   `deploy/uninstall.sh` removes them. Logs land in `logs/` (the jukebox log
   rotates at 5 MB).
6. **party-lights:** point it at the jukebox's port and the lightweight
   endpoint, in `party-lights/config/settings.yaml`:
   ```yaml
   jukebox:
     url: http://localhost:5001
   ```
   It polls `/api/status`, which still answers without a party code with only
   the current track. `/api/now` returns the same `current_track` and is the
   lighter choice once party-lights' client is switched to it.

## Party night

1. Plug the Mac into power (on battery it may sleep).
2. Open Spotify on the Mac. Set the Mac's output to the Multi-Output Device.
3. Run the check and fix anything marked ✗:
   ```sh
   .venv/bin/python deploy/preflight.py
   ```
4. Open `https://party.example.com/host`. Log in and connect Spotify if asked.
   Check the playback device in Settings (if the Mac is the only computer
   running Spotify, it's pinned automatically), then click **Start new party**. That
   gives you a fresh party code.
5. Put the QR code (or **Copy invite link**) where guests can see it, and open
   **Open TV screen** on the TV.

## Alerts on Discord

The watchdog service (`deploy/watchdog.py`, installed by `install.sh`) checks
everything every 15 seconds. When something breaks it posts to a Discord channel,
and it posts again when the problem clears. It watches the three services, the
jukebox's health and Spotify, the public link, the lights (frozen, stuttering,
no DMX adapter, deaf to the music), power, disk space, and new crashes in `logs/`.
It never restarts or changes anything.

Short blips (a service restarting, one slow answer) are ignored. A problem has
to last 30 seconds to a couple of minutes, depending on what it is. Critical
alerts mention everyone and repeat every 15 minutes until fixed. Everything
that happened in one check goes out as one message.

**Setup (about 5 minutes, once):**

1. In Discord, create a server for the party crew and invite the 2–4 people
   who should get alerts. Keep it private: anyone in it sees the alerts.
2. Make an `#alerts` channel. Open its settings, then **Integrations →
   Webhooks → New Webhook**, and copy the webhook URL. Treat that URL like a
   password, because anyone holding it can post as the watchdog.
3. Add it to `.env`:
   ```sh
   PARTY_ALERTS_WEBHOOK=https://discord.com/api/webhooks/...
   ```
   The watchdog re-reads `.env` on every check, so there's no need to restart it.
   Check it got there: `.venv/bin/python deploy/watchdog.py --test`
4. Everyone in the server: open the server's notification settings, choose
   **All Messages**, and allow Discord through any Focus mode you'll have on
   at the party.
5. *Optional, and recommended:* if this Mac dies or sleeps, the watchdog dies
   with it. Make a free check at healthchecks.io with a 1-minute period and a
   2-minute grace, add a Discord integration to it using the same webhook,
   and put its ping URL in `.env` as `PARTY_ALERTS_HEARTBEAT_URL`.

**Day to day:**

- `.venv/bin/python deploy/watchdog.py --once` shows what it sees right now.
- `touch logs/watchdog.mute` silences Discord while you work on the code
  (restarting a service on purpose alerts too). `rm logs/watchdog.mute` turns
  it back on. Alerts still go to `logs/watchdog.log` while muted.

## After the party: share the photos

Guests' disposable camera shots wait in `photos/` on the Mac until you review
them. Nothing is shared until you approve it.

1. Open `https://party.example.com/host/photos` (same host password). Click a
   photo to see it full size. **A** approves it, **R** rejects it, and the arrow
   keys move between photos.
2. Build the gallery from the approved photos:
   ```sh
   .venv/bin/python deploy/export_photos.py --title "Halloween 2026"
   ```
   It writes `exports/<party code>-photos/` with the photos, an `index.html`
   gallery and `all-photos.zip`. Approved photos only, with all metadata
   (including GPS location) already removed when they were uploaded.
3. Share it, either way:
   - **Google Drive:** upload the `photos/` folder (or the zip) and share the
     folder link. It works without the Mac staying on.
   - **Cloudflare Pages:** run `npx wrangler login` once, then
     ```sh
     .venv/bin/python deploy/export_photos.py --title "Halloween 2026" --deploy-cloudflare party-photos-2026
     ```
     and send the `https://party-photos-2026.pages.dev` link it prints. Anyone
     with the link can see the gallery, and it asks search engines not to
     index it. Pick a project name that's hard to guess.

Rerun the export after changing a review and the gallery is rebuilt from
scratch. To pull a photo after sharing it, reject it, export again and
re-upload or redeploy.

## Things to know

- **The camera needs the https link.** Phones only allow a web page to use the
  camera over HTTPS, so guests on the tunnel address can shoot. Guests on a
  plain `http://192.168.x.x` local address can't, and are told so.

- **The party code is the key.** The QR and invite link carry it; the plain
  URL alone only shows what's playing. Starting a new party changes it, so a
  link shared last time stops working.
- **Use the public address for the host panel.** With `BEHIND_PROXY=1` the
  session cookie is HTTPS-only and the app listens only on localhost.
- **Lid closed = asleep** unless an external display, keyboard and power are
  attached (clamshell mode). Keep the lid open.
- **Microphone permission for party-lights under launchd.** macOS may block a
  background Python from reading BlackHole. If the lights don't react when run
  by `--with-lights`, run party-lights from Terminal once and allow microphone
  access, or keep running it from Terminal.
- **No DMX adapter, no crash.** If the serial port in party-lights'
  `settings.yaml` isn't there when the service starts, it runs with the null
  driver. Plug the adapter in, then restart it:
  `launchctl kickstart -k gui/$(id -u)/com.partyjukebox.lights`
- **One jukebox process only.** The queue manager keeps its state in memory;
  never run two copies against the same database.
