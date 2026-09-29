# Stock Watcher

A self-hosted restock alert watcher with **ntfy** push notifications. Monitor product availability across multiple stores, receive instant alerts when items come back in stock, and track price changes—no data collection, no tracking, 100% yours.

## Features

- **Auto-detect availability**: Intelligent detection using JSON-LD, schema.org microdata, meta tags, and button/text heuristics
- **59 supported stores**: dedicated checks for Target (delivery and in-store pickup), Best Buy, Amazon, Walmart,
  Newegg, Micro Center, Nvidia, PlayStation Direct, Pokémon Center, LEGO and more (see [Supported stores](#supported-stores)),
  plus Shopify / Salesforce Commerce / BigCommerce / Magento / WooCommerce / OpenCart shops auto-detected
- **HotStock-style alerts**: price limits, official-seller-only, pickup near your ZIP, mute stores, track one product at
  several stores, restock history, and an **Add to cart** button on alerts
- **Custom rules**: Per-item CSS selectors or text patterns for complex product pages
- **Apple specialist**: Track in-store pickup availability at stores near your ZIP code and 2-hour courier delivery
- **Push notifications**: Instant alerts via **ntfy** (can be self-hosted)
- **Notification center**: In-app history of all alerts with read/unread tracking
- **Multi-user support**: Role-based access (admin/user) with per-user settings and privacy
- **Product images**: Automatically download and display product photos
- **Dark UI**: Beautiful dark-first design with light mode toggle
- **Portable**: Single container, one-click Synology deployment

## Architecture

**Stock Watcher** is a self-contained container running:

- **FastAPI** backend serving REST API (`/api/*`)
- **In-process scheduler** polling items at configurable intervals (default 2 minutes)
- **SQLite** database + image storage on persistent `/data` volume
- **React + Vite** single-page app (SPA) served at `/`

The container uses:
- **Python 3.12** with FastAPI, SQLAlchemy 2.0, Playwright (Chromium fallback for JavaScript-heavy pages)
- **Node.js 22** to build the frontend during image creation
- One **uvicorn worker** (scheduler runs in-process, no separate queue)

## Deployment

### Synology Quick Start (SSH)

Paths assume `/volume2/docker/stock-watcher`; adjust `docker-compose.yml` if yours differ.

1. Put `docker-compose.yml` in `/volume2/docker/stock-watcher/` and start it (the image is public, no login needed):
   ```bash
   cd /volume2/docker/stock-watcher && sudo docker compose up -d
   ```
2. Open the app through your HTTPS reverse-proxy address and create the admin account.

### Updating

Every push to `main` builds a new image on GHCR (public, so no login is needed to pull it).

**Automatic (Watchtower):** reuse the Watchtower you already run; don't start a second one, because a new
instance stops the existing one. `docker-compose.yml` labels the container
`com.centurylinklabs.watchtower.enable=true` and `com.centurylinklabs.watchtower.scope=homelab`. Set the scope to
your Watchtower's `WATCHTOWER_SCOPE`, or delete that line if it has none. Updates arrive within Watchtower's
`WATCHTOWER_POLL_INTERVAL`.

**From the app:** admins see **Settings → Updates** with the running and latest versions. For the
**Update now** button, give the app your Watchtower's HTTP API (Watchtower needs `WATCHTOWER_HTTP_API_UPDATE=true`):
`WATCHTOWER_URL=http://<nas-ip>:8080` and `WATCHTOWER_TOKEN=<its WATCHTOWER_HTTP_API_TOKEN>`. The app only asks
Watchtower to update this one image; it never gets access to Docker itself.

**Manual:**

```bash
cd /volume2/docker/stock-watcher && sudo docker compose pull && sudo docker compose up -d
```

Data in `/volume2/docker/stock-watcher/data` is kept.

### Reverse Proxy Setup

If using a reverse proxy (e.g., Synology DSM reverse proxy):

1. **Synology DSM** → Control Panel → Login Portal → Advanced
2. Create a reverse proxy rule:
   - Forward HTTPS host (e.g., `stockwatcher.example.com`)
   - To `http://localhost:8095` (or your container's host IP)
3. Leave `COOKIE_SECURE=auto`: the login cookie is marked Secure when the request came in over HTTPS (reverse proxy / Cloudflare Tunnel via `X-Forwarded-Proto`) and still works over plain-http LAN

## Configuration

### Environment Variables

| Variable | Default | Notes |
|---|---|---|
| `DATA_DIR` | `/data` | SQLite database and images location |
| `SECRET_KEY` | auto-generated | Persisted to `$DATA_DIR/secret.key` |
| `COOKIE_SECURE` | `auto` | `auto` = Secure cookie only over HTTPS; `true`/`false` to force |
| `SESSION_DAYS` | `30` | Session cookie expiry |
| `MIN_INTERVAL_SECONDS` | `60` | Minimum check interval per item (seconds) |
| `CHECK_CONCURRENCY` | `4` | Concurrent checks during scheduler loop |
| `STATIC_DIR` | `/app/static` | Built SPA location (inside container) |
| `TZ` | `UTC` | Timezone for scheduler and logs |
| `ENABLE_BROWSER` | `true` | Allow the real-browser (Chromium) fallback for JS pages and bot-protected stores |
| `BROWSER_MODE` | `auto` | `auto` = headed on a screen, or on a built-in virtual display (Xvfb) in Docker; `headed`; `headless` |
| `BROWSER_CHANNEL` | bundled Chromium | `chrome` = the installed Google Chrome (desktop only; not in the Docker image) |
| `BROWSER_CDP_URL` | – | Use a Chrome you run instead of launching one, e.g. `http://chromium:9223` (see below) |
| `BROWSER_CHALLENGE_WAIT` | `20` | Seconds a "Just a moment…" / "Access Denied" check gets to clear before giving up |
| `BROWSER_CONCURRENCY` | `2` | Browser tabs open at once |
| `BROWSER_PROFILE_DIR` | `$DATA_DIR/browser-profile` | Browser profile; keeps the cookies stores set after their bot check |
| `STOCKWATCHER_IMPERSONATE` | `true` | Use Chrome TLS impersonation (curl_cffi) for bot-protected stores such as Best Buy; `false` to use plain HTTP |
| `BESTBUY_API_KEY` | – | Optional [Best Buy developer key](https://developer.bestbuy.com/): most reliable Best Buy checks, plus store pickup |
| `KROGER_CLIENT_ID` / `KROGER_CLIENT_SECRET` | – | Optional [Kroger developer app](https://developer.kroger.com/): stock at your nearest Kroger-family store |

### Bot-protected stores (real browser)

Stores behind Cloudflare, Akamai, PerimeterX or Imperva block plain requests and headless browsers.
Stock Watcher uses a real Chromium ([patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python),
a Playwright build without automation tells). In Docker it runs headed on a virtual display, keeps its
cookies in `/data/browser-profile`, and gives a bot check up to `BROWSER_CHALLENGE_WAIT` seconds to pass.
Stores known to need it go straight to the browser.

If a store still says *Blocked by bot protection*, point Stock Watcher at a Chrome you can see and
solve the captcha in once. Add to the same compose file:

```yaml
  chromium:
    image: lscr.io/linuxserver/chromium:latest
    container_name: chromium
    restart: unless-stopped
    shm_size: 1gb
    security_opt: [seccomp:unconfined]
    environment:
      TZ: America/New_York
      CUSTOM_USER: admin        # login for the web desktop
      PASSWORD: change-me
      CHROME_CLI: "--remote-debugging-port=9222 --user-data-dir=/config/stockwatcher-profile"
    volumes:
      - /volume2/docker/chromium:/config   # keeps its cookies
    ports:
      - "192.168.1.10:3001:3001"           # web desktop, your NAS's LAN IP only
  chromium-cdp:  # Chrome only listens on its own localhost: forward it to stock-watcher (not published)
    image: alpine/socat
    restart: unless-stopped
    network_mode: "service:chromium"
    command: tcp-listen:9223,fork,reuseaddr tcp-connect:127.0.0.1:9222
```

Set `BROWSER_CDP_URL: "http://chromium:9223"` on `stock-watcher`, recreate both, open
`https://192.168.1.10:3001`, visit the store and pass its check. Checks then run in that Chrome, with its cookies.

### ntfy Setup (Notifications)

1. **Install ntfy app**:
   - iOS: [App Store](https://apps.apple.com/app/ntfy/id1615465896)
   - Android: [Google Play](https://play.google.com/store/apps/details?id=io.heckel.ntfy) or F-Droid
   - Web: [ntfy.sh](https://ntfy.sh)

2. **Choose a topic** (hard to guess, e.g., `stock-watcher-abc123xyz`):
   - In ntfy app: **Subscribe** → enter your topic
   - Don't share the topic publicly (it's unencrypted)

3. **Configure in Stock Watcher**:
   - Log in → **Settings**
   - Enter your ntfy server (default `https://ntfy.sh`) and topic
   - Enter access token if using authentication (leave blank for public)
   - Click **Test Notification** to verify
   - Set priority (1–5, default 4)

4. **Optional: Self-hosted ntfy**:
   - Deploy ntfy server (see [ntfy.sh docs](https://docs.ntfy.sh/))
   - Point Stock Watcher to your server URL
   - Use access tokens for privacy

## Alerts

- You get **one alert per restock**: when an item goes from unavailable to available (in the app and via ntfy).
- More stores or delivery options opening up while it's already available don't alert again, and nothing
  alerts when an item sells out.
- After an alert, that item's alerts **pause** (it keeps being checked). Click **Alerts paused · Re-arm** on the
  item's card or page to get the next one.
- **Check all** on the dashboard re-checks every active item immediately.
- **Purchased:** hit **Bought it** on an in-stock card (or **Mark purchased** in any item's menu). The item stops
  being checked and moves to the **Purchased** page with the date and price; **Watch again** brings it back.
- **Every restock** (Settings → Alert behaviour): alerts stay armed, so you're told about each new restock instead
  of just the first one. An **alert sound** plays with browser notifications.
- **Price limit** (per item): in stock above your limit doesn't alert; dropping under it does.
- **Official seller only** (Amazon, Walmart, Target, Best Buy, Newegg, Sam's Club; on by default): marketplace-only
  offers count as out of stock.
- **Mute a store** on the **Stores** page to stop its alerts without deleting items.
- **Browser notifications** (Settings → Notifications) pop up on your computer while Stock Watcher is open in a
  tab. They need the https address. Use ntfy for alerts when the app isn't open.

## Supported stores

The **Stores** page lists them all: AMD, ASUS, Ace Graphics Cards, Adorama, Amazon, Antonline, B&H Photo Video, BJ's,
Bandai Namco, Best Buy, Canon, Consutronix, Costco, Dell, Disney, EVGA, Fujifilm, GameFly, GameStop, Gigabyte, Govee,
Hallmark, Home Depot, Jazwares, Kohl's, Kroger (and its banners), LG, LEGO, Leica, Lenovo, MSI, Mattel, Meijer,
Micro Center, Microsoft, Microsoft Xbox, NYXI, NeutronUSA, Newegg, NextWarehouse, Ninja Kitchen, Nintendo, Nvidia,
Oculus / Meta Quest, Office Depot, POP MART, Play-Asia, PlayStation Direct, Pokémon Center, QVC, Roberts Camera,
Sam's Club, StockX, Target (Delivery and Pickup), Toys"R"Us, Verizon, Walmart, Zotac and eBay. Any other store still
works through the generic detection.

- **Store options** appear on the item form when they apply: **Delivery / Pickup / Both** with a ZIP and distance
  (Target, Best Buy with an API key, Kroger, Micro Center store), and **Official seller only**.
- **Track at another store** on an item's page adds the same product at another retailer and shows a comparison
  table (status, price, last in stock).
- **Waiting rooms** (queue-it and similar) show as *Waiting room active — drop may be live* rather than an error.
- Many stores fight bots. If one keeps failing, see **Verify stores from your computer** below.

### Verify stores from your computer

`tools/site-probe/` runs the exact checker code on your own machine and home connection, and records what each store
returned. `./tools/site-probe/run.sh sweep` checks a sample product for every store and writes
`probe-output/report.md`; `run.sh bundle` makes a zip you can attach to an issue so the checks can be fixed with real
pages. See [tools/site-probe/README.md](tools/site-probe/README.md).

## Tracking Apple Availability

### Add an Apple Item

1. **Add item** → paste an Apple buy-page URL (e.g. `https://www.apple.com/shop/buy-iphone/iphone-17-pro`).
   The form switches to Apple mode and lists the models it finds on the page — tick the ones you want.
2. If no models are listed, add part numbers manually (e.g. `MG8H4LL/A`).
3. Set your ZIP code and max distance (defaults come from **Settings → Defaults**).
4. Choose what to watch: **In-store pickup**, **2-hour delivery**, or both.
   "Only alert for same-day pickup" is on by default; turn it off to also be alerted for later pickup dates.

### How It Works

- Each check queries Apple's pickup/delivery availability for your parts around your ZIP.
- The item page shows every nearby store with a per-model pill (Today / Later / Unavailable) and a 2-hour delivery panel.
- You get one alert per check listing every **newly** available store/delivery option — no repeats while it stays available.
- If Apple blocks the plain request, Stock Watcher retries through the built-in headless Chromium.
- Keep the interval at 2 minutes or more to avoid rate limiting.

### Verify after first deploy

Apple's endpoint couldn't be tested live during development. After deploying, add an item and check the logs
(`docker logs stock-watcher`) for `apple fulfillment blocked`, and confirm the store table matches apple.com.
The 2-hour courier wording is detected heuristically — if it never triggers where Apple offers it, open an issue
with a saved response.

## Local Development

### Backend

```bash
cd backend

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements-dev.txt

# Run with auto-reload
export DATA_DIR=./data COOKIE_SECURE=false
uvicorn app.main:app --reload
```

Backend runs on `http://localhost:8000`. API is at `/api/*`.

### Frontend

In a new terminal:

```bash
cd frontend

# Install dependencies
npm ci

# Start dev server with proxy to /api
npm run dev
```

Frontend dev server runs on `http://localhost:5173` and proxies `/api` requests to the backend.

## Troubleshooting

### `docker compose pull` says "unauthorized" or "denied"

- The GHCR package must be public: GitHub → your profile → Packages → stock-watcher → Package settings →
  Change visibility → Public (making the repo public doesn't change the package).

### Container won't start / Chromium crashes

- Increase `shm_size` in docker-compose (set to `1gb` by default)
- Chromium needs shared memory for rendering
- Restart the container after adjusting
- The log line `browser started: {...}` shows the engine and mode (`headed-xvfb` is the normal Docker mode).
  Set `BROWSER_MODE: "headless"` to rule out the virtual display.
- A crashed run's profile locks are cleared automatically. If the profile itself is broken, stop the
  container and delete `data/browser-profile/`; it only holds store cookies.

### A store says "Blocked by bot protection"

- Some stores clear after the first check stores a cookie in the browser profile. Give it a few checks.
- Raise `BROWSER_CHALLENGE_WAIT` (e.g. `40`) for slow challenges.
- Use a Chrome you can see (`BROWSER_CDP_URL`, see [Bot-protected stores](#bot-protected-stores-real-browser))
  and solve the captcha there once.
- Run the [site probe](tools/site-probe/README.md) on your computer with `--chrome` or `--cdp` to see what the store sends.

### "Cannot log in" / Sessions not persisting

- Ensure `/volume2/docker/stock-watcher/data` is writable
- Check Synology Container Manager logs for permission errors

### Reset admin password

Coming soon: CLI utility to reset credentials. For now, you can connect to the running container and reset via database (details to follow).

## License

Stock Watcher is self-hosted software by you, for you.
