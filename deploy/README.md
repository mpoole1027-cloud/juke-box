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
   Pin the playback device in Settings, then click **Start new party**. That
   gives you a fresh party code.
5. Put the QR code (or **Copy invite link**) where guests can see it, and open
   **Open TV screen** on the TV.

## Things to know

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
- **One jukebox process only.** The queue manager keeps its state in memory;
  never run two copies against the same database.
