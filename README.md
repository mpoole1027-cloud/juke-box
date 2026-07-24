# Party Jukebox

A Halloween party web app that lets guests scan a QR code and queue Spotify songs through the speakers. Includes a downvote/skip system with an automatic ban for repeat offenders, a host control panel, and a TV display mode — all wrapped in a retro neon jukebox aesthetic.

---

## Requirements

- Python 3.8+
- A **Spotify Premium** account (required for playback control)
- The Spotify app open and active on whatever device/speaker you'll use at the party

---

## 1. Create a Spotify Developer App

1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) and log in
2. Click **Create app**
3. Fill in any name and description
4. Set the **Redirect URI** to: `http://localhost:5000/auth/callback`
5. Save the app, then copy your **Client ID** and **Client Secret** — you'll need these in the next step

---

## 2. Configure Your Environment

Copy the example env file and fill it in:

```powershell
Copy-Item .env.example .env
```

Then open `.env` and set these values:

```env
SPOTIFY_CLIENT_ID=       # from your Spotify developer app
SPOTIFY_CLIENT_SECRET=   # from your Spotify developer app
SPOTIFY_REDIRECT_URI=http://localhost:5000/auth/callback

HOST_PASSWORD=           # password you'll use to log into the host panel
PARTY_URL=               # your machine's local IP + port, e.g. http://192.168.1.50:5000
FLASK_SECRET_KEY=        # any long random string, e.g. halloween2024xk39fj

DOWNVOTE_THRESHOLD=7     # how many downvotes to skip a song
MAX_QUEUE_PER_USER=2     # max songs one person can have in the queue at once
SKIP_BAN_THRESHOLD=2     # how many skips before a user is banned
PORT=5000
```

**Finding your local IP (for `PARTY_URL`):** Run `ipconfig` in PowerShell and look for the IPv4 address under your Wi-Fi adapter (e.g. `192.168.1.50`). Guests must be on the same Wi-Fi network.

---

## 3. Install Dependencies

```powershell
pip install -r requirements.txt
```

---

## 4. Run the App

```powershell
python app.py
```

The server starts on port 5000 by default.

---

## 5. Connect Spotify (do this before the party)

1. Open `http://localhost:5000/host` in your browser
2. Enter your host password
3. Click **Connect Spotify** and authorize the app
4. Start playing something on Spotify on the device/speaker you'll use at the party — Spotify must have an active device for queue control to work

---

## 6. Party Time

| URL | Purpose |
|-----|---------|
| `http://localhost:5000/` | Guest jukebox (or share via QR) |
| `http://localhost:5000/host` | Host control panel |
| `http://localhost:5000/tv` | TV display — put this on your party screen |
| `http://localhost:5000/qr` | QR code image to print or display |

- Open `/tv` on a laptop connected to your TV or projector — it shows now playing, the upcoming queue, reactions, and the banned list
- Display the QR code (`/qr`) somewhere guests can scan it

---

## How It Works

### For guests
- Scan the QR code to open the jukebox on their phone
- Search for a song and tap **Queue It**
- Downvote the current song with the big red button — 7 downvotes (configurable) skips it
- React to the current song with 🔥 or ❤️
- Each person can have at most 2 songs in the queue at once
- The same song can't be queued twice in one night

### Bans
- If a guest's song gets skipped by downvotes **twice**, they are automatically banned from queuing for the rest of the night
- Banned guests see a message on their screen and their anonymous nickname (e.g. "Disco Wombat") is revealed in the Hall of Shame — visible to everyone including on the TV display

### For the host
- Log into `/host` to see all guests, their stats, and ban/unban anyone manually
- Adjust the downvote threshold and queue limits live during the party
- Force-skip the current song instantly
- Clear the entire pending queue if needed

---

## Troubleshooting

**Spotify says "No active device"**
Start playing any song on the speaker/device you want to use, then try queuing again. Spotify requires an active device for API-based queue control.

**Guests can't reach the app**
Make sure they're on the same Wi-Fi network. Check that `PARTY_URL` in your `.env` matches your machine's current local IP (`ipconfig` → Wi-Fi IPv4 address).

**Spotify auth fails on redirect**
Double-check that the Redirect URI in your Spotify developer dashboard exactly matches `SPOTIFY_REDIRECT_URI` in your `.env` — including `http://` vs `https://` and the port.

**Songs aren't advancing automatically**
The background thread polls Spotify every 5 seconds. If playback stalls, skip manually from the host panel or from Spotify directly.
