# Party Jukebox

A Halloween party web app that lets guests scan a QR code and queue Spotify songs through the speakers. Includes a downvote/skip system with an automatic ban for repeat offenders, a host control panel, and a TV display mode — all wrapped in a retro neon jukebox aesthetic.

---

## Requirements

- Python 3.8+
- A **Spotify Premium** account (required for playback control)
- The Spotify app open and active on whatever device/speaker you'll use at the party

---

> **Hosting a party for guests on any network?** Follow [deploy/README.md](deploy/README.md):
> a Cloudflare Tunnel gives the jukebox a public HTTPS address, launchd keeps it
> running, and `deploy/preflight.py` checks everything before guests arrive.
> The steps below are the local setup it builds on.

---

## 1. Create a Spotify Developer App

1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) and log in
2. Click **Create app**
3. Fill in any name and description
4. Set the **Redirect URI** to: `http://127.0.0.1:5001/auth/callback` (Spotify only allows plain `http` for loopback addresses; a public address needs `https`)
5. Save the app, then copy your **Client ID** and **Client Secret** — you'll need these in the next step

---

## 2. Configure Your Environment

Copy the example env file and fill it in:

```sh
cp .env.example .env
```

Then open `.env` and set these values:

```env
SPOTIFY_CLIENT_ID=       # from your Spotify developer app
SPOTIFY_CLIENT_SECRET=   # from your Spotify developer app
SPOTIFY_REDIRECT_URI=http://127.0.0.1:5001/auth/callback

HOST_PASSWORD=           # host panel password: 8+ characters, not the default
PARTY_URL=               # the address guests use, e.g. http://192.168.1.50:5001
FLASK_SECRET_KEY=        # 32+ random chars: python -c "import secrets; print(secrets.token_hex(32))"

DOWNVOTE_THRESHOLD=7     # how many downvotes to skip a song
MAX_QUEUE_PER_USER=2     # max songs one person can have in the queue at once
SKIP_BAN_THRESHOLD=2     # how many skips before a user is banned
PORT=5001                # not 5000: macOS AirPlay Receiver owns that port
```

The three thresholds only seed the first run; after that, change them in the host panel.

The app **refuses to start** while `FLASK_SECRET_KEY` is missing or short, or the host password is a default, because either would let anyone take over the host panel. On a private machine, `JUKEBOX_DEV=1` turns that into a warning.

**Finding your local IP (for `PARTY_URL` without a tunnel):** `ipconfig getifaddr en0` on a Mac. Guests must then be on the same Wi-Fi network.

---

## 3. Install Dependencies

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

---

## 4. Run the App

```sh
.venv/bin/python app.py
```

It serves with waitress on port 5001 by default, as **one process**: the queue manager keeps playback state in memory, so never run two copies.

---

## 5. Connect Spotify (do this before the party)

1. Open `http://127.0.0.1:5001/host` in your browser
2. Enter your host password
3. Click **Connect Spotify** and authorize the app
4. Start playing something on Spotify on the device/speaker you'll use at the party — Spotify must have an active device for queue control to work

---

## 6. Party Time

| URL | Purpose |
|-----|---------|
| `/host` | Host control panel: QR code, party code, invite link, TV link |
| `/?p=CODE` | Guest jukebox (the QR code and invite link open this) |
| `/tv?p=CODE` | TV display (use **Open TV screen** in the host panel) |
| `/host/photos` | Review disposable camera photos after the party |
| `/api/now` | Just the current track, for party-lights and other local tools |
| `/healthz` | Health check used by `deploy/preflight.py` |

- Guests need tonight's **party code**, which the QR code and invite link carry. The plain URL only shows what's playing. **Start new party** in the host panel changes the code.
- Open the TV link on a laptop connected to your TV or projector. It shows now playing, the upcoming queue, reactions and the banned list.

---

## How It Works

### For guests
- Scan the QR code to open the jukebox on their phone
- Search for a song and tap **Queue It**
- Downvote the current song with the big red button — 7 downvotes (configurable) skips it
- React to the current song with 🔥 or ❤️
- Upvote a queued song 👍 — each upvote moves it up one spot, up to 3 spots. After that it shows **⏫ Max boost** and guests can still upvote it, but it won't move any further
- Each person can have at most 2 songs in the queue at once
- The same song can't be queued twice in one night

### Bans
- If a guest's song gets skipped by downvotes **twice**, they are automatically banned from queuing for the rest of the night
- Banned guests see a message on their screen and their anonymous nickname (e.g. "Disco Wombat") is revealed in the Hall of Shame — visible to everyone including on the TV display

### Disposable camera
- Guests open the camera from the jukebox page and take photos through a live viewfinder
- A shot is never shown back to them: it goes straight to the Mac to "develop" until after the party
- Each guest gets a roll of film (24 shots by default, changeable in the host panel) per party
- Shots taken while the phone is offline wait on the phone and upload once it reconnects
- Uploads are re-encoded on arrival, which strips metadata like GPS location
- After the party, review them at `/host/photos` and export the approved ones to Google Drive or Cloudflare Pages (see [deploy/README.md](deploy/README.md))
- The camera needs the https (tunnel) address; phones block it on a plain `http://` local IP

### For the host
- Log into `/host` to see all guests, their stats, and ban/unban anyone manually
- Adjust the downvote threshold and queue limits live during the party
- Force-skip the current song instantly
- Clear the entire pending queue if needed

---

## Troubleshooting

**Spotify says "No active device"**
The host panel shows this as a banner. Start playing any song on the speaker/device you want to use, then try queuing again. Spotify requires an active device for API-based queue control.

**Guests can't reach the app**
Without a tunnel they must be on your Wi-Fi, and `PARTY_URL` must match your machine's current IP. With a tunnel, run `deploy/preflight.py`.

**Guests see "Join the party"**
They opened the plain URL, or the party code changed. Have them scan the QR code again or type the code shown in the host panel.

**Spotify auth fails on redirect**
Double-check that the Redirect URI in your Spotify developer dashboard exactly matches `SPOTIFY_REDIRECT_URI` in your `.env` — including `http://` vs `https://` and the port.

**Songs aren't advancing automatically**
The background thread polls Spotify every 3 seconds. If something blocks playback (no device, Spotify down), the host panel shows a banner saying what. A song whose play attempt failed is retried automatically once the problem clears.
