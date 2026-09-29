# Stock Watcher

A self-hosted restock alert watcher with **ntfy** push notifications. Monitor product availability across multiple stores, receive instant alerts when items come back in stock, and track price changes—no data collection, no tracking, 100% yours.

## Features

- **Auto-detect availability**: Intelligent detection using JSON-LD, schema.org microdata, meta tags, and button/text heuristics
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

### Synology Container Manager (Quick Start)

1. **Create directories**:
   ```bash
   # On Synology via SSH
   mkdir -p /volume1/docker/stock-watcher/data
   mkdir -p /volume1/docker/watchtower
   ```

2. **Create GitHub Classic PAT**:
   - Go to GitHub → Settings → Developer settings → Personal access tokens (classic)
   - Create a token with `read:packages` scope
   - Copy the token

3. **Create Watchtower config**:
   ```bash
   # On Synology via SSH
   GITHUB_USERNAME="your-username"
   PAT="ghp_your_token_here"
   AUTH=$(echo -n "$GITHUB_USERNAME:$PAT" | base64)
   cat > /volume1/docker/watchtower/config.json <<EOF
   {
     "auths": {
       "ghcr.io": {
         "auth": "$AUTH"
       }
     }
   }
   EOF
   chmod 600 /volume1/docker/watchtower/config.json
   ```

4. **Deploy via Container Manager**:
   - Open **Container Manager** → **Project**
   - Click **Create**
   - Paste the `docker-compose.yml` content
   - Adjust `TZ` timezone if needed
   - Click **Deploy**

5. **Initial setup**:
   - Open `http://<synology-ip>:8080` in your browser
   - You'll see a setup screen to create the admin account
   - Enter username and password, then click **Create Admin**
   - Log in—you're ready to add items

### Auto-Updates

Once deployed, automatic updates work like this:

1. Push a commit to `main` branch
2. GitHub Actions builds a multi-arch image (`linux/amd64`, `linux/arm64`) and pushes to GHCR
3. Watchtower polls the registry every 5 minutes
4. When a new image is detected, Watchtower pulls it and restarts the container
5. Your data persists in `/volume1/docker/stock-watcher/data`

**Note**: After the first successful workflow run, the GHCR package inherits the private visibility from the repository. Ensure your GitHub PAT has `read:packages` scope.

### Reverse Proxy Setup

If using a reverse proxy (e.g., Synology DSM reverse proxy):

1. **Synology DSM** → Control Panel → Login Portal → Advanced
2. Create a reverse proxy rule:
   - Forward HTTPS host (e.g., `stockwatcher.example.com`)
   - To `http://localhost:8080` (or your container's host IP)
3. **Keep `COOKIE_SECURE=true`** when behind HTTPS—the app automatically trusts `X-Forwarded-*` headers

## Configuration

### Environment Variables

| Variable | Default | Notes |
|---|---|---|
| `DATA_DIR` | `/data` | SQLite database and images location |
| `SECRET_KEY` | auto-generated | Persisted to `$DATA_DIR/secret.key` |
| `COOKIE_SECURE` | `true` | Set to `false` for plain-HTTP LAN testing |
| `SESSION_DAYS` | `30` | Session cookie expiry |
| `MIN_INTERVAL_SECONDS` | `60` | Minimum check interval per item (seconds) |
| `CHECK_CONCURRENCY` | `4` | Concurrent checks during scheduler loop |
| `STATIC_DIR` | `/app/static` | Built SPA location (inside container) |
| `TZ` | `UTC` | Timezone for scheduler and logs |
| `ENABLE_BROWSER` | `true` | Allow Playwright Chromium fallback for JS pages |

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

## Tracking Apple Availability

### Add an Apple Item

1. **From an Apple product page**:
   - Copy the URL (e.g., `https://www.apple.com/shop/buy-iphone/iphone-15-pro`)
   - In Stock Watcher: **Add Item** → paste the URL
   - Click **Preview** to auto-detect part numbers, image, price, and availability
   - Set your ZIP code and search radius (default 25 miles)
   - Save

2. **Or manually by part number**:
   - Find the part number (e.g., `MG8H4LL/A` for iPhone 15 Pro 256GB)
   - Create item with `kind: apple`, provide part numbers and ZIP code
   - Stock Watcher will track pickup and 2-hour delivery availability

### How It Works

- Stock Watcher queries the **Apple in-store pickup API** every check interval
- Shows available stores near your ZIP with distance and estimated pickup time
- Also tracks **2-hour delivery** availability
- Sends alerts when availability changes (e.g., "Pickup available at 2 stores")
- **Note**: Apple may rate-limit. Keep check interval ≥ 2 minutes to avoid being blocked.

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

### Watchtower says "unauthorized"

- Check that `/volume1/docker/watchtower/config.json` exists and is readable
- Verify the base64 auth string: `echo -n "username:token" | base64`
- Ensure the GitHub PAT has `read:packages` scope

### Container won't start / Chromium crashes

- Increase `shm_size` in docker-compose (set to `1gb` by default)
- Chromium needs shared memory for rendering
- Restart the container after adjusting

### "Cannot log in" / Sessions not persisting

- Ensure `/volume1/docker/stock-watcher/data` is writable
- Check Synology Container Manager logs for permission errors

### Reset admin password

Coming soon: CLI utility to reset credentials. For now, you can connect to the running container and reset via database (details to follow).

## License

Stock Watcher is self-hosted software by you, for you.
