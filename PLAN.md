# Stock Watcher — Plan & Contract

Self-hosted restock watcher. Polls product pages every few minutes, detects
restocks, and pushes alerts via **ntfy**. Specialized Apple support: in-store
pickup availability at stores near a ZIP, plus 2-hour courier delivery.

This file is the **source of truth** shared by every contributor/agent.
If you need to deviate from a contract here, note it in your final report.

## Decisions

| Area | Choice |
|---|---|
| Backend | Python 3.12, FastAPI, SQLAlchemy 2.0 (sync, SQLite), Pydantic v2, httpx, BeautifulSoup/lxml, Playwright (Chromium) fallback for JS pages, argon2-cffi |
| Frontend | React 18 + TypeScript + Vite, Tailwind CSS, React Router, TanStack Query, lucide-react icons, sonner toasts. Dark-first modern UI with light toggle |
| Storage | SQLite + images on the `/data` volume |
| Container | Single image: FastAPI serves API (`/api/*`) and built SPA (everything else). Single uvicorn worker (scheduler lives in-process) |
| Registry | GHCR (`ghcr.io/loswastaken/stock-watcher`), **private** repo → Watchtower needs a GitHub PAT (`read:packages`) |
| Updates | GitHub Actions builds `linux/amd64,linux/arm64` on push to `main`; Watchtower polls every 300 s |
| Accounts | First visitor creates the admin (setup screen). After that, only admins create users. No open registration |
| Detection | Auto (JSON-LD/schema.org `availability`, microdata, meta tags, button/text heuristics) + optional per-item CSS selector / text rules |

## Repo layout

```
backend/
  app/
    main.py            # FastAPI app, lifespan starts scheduler, serves SPA from STATIC_DIR
    config.py          # env settings
    db.py              # engine/session, create_all + lightweight migrations
    models.py          # ORM models
    schemas.py         # Pydantic API schemas
    security.py        # password hashing, sessions, deps (current_user, admin_required), login rate limit
    scheduler.py       # due-item loop, notification diffing
    notifier.py        # ntfy client
    images.py          # download/store/serve item images
    routers/           # auth, users, settings, items, notifications, apple, stats, images, health
    checkers/          # OWNED BY CHECKERS AGENT — see "Checker contract"
  tests/
  requirements.txt
frontend/              # Vite app, builds to frontend/dist
Dockerfile
docker-compose.yml     # Synology example: app + watchtower
.github/workflows/docker.yml
README.md
```

## Config (env vars)

| Var | Default | Notes |
|---|---|---|
| `DATA_DIR` | `/data` | SQLite at `$DATA_DIR/stockwatcher.db`, images in `$DATA_DIR/images/` |
| `SECRET_KEY` | auto-generated & persisted to `$DATA_DIR/secret.key` | |
| `COOKIE_SECURE` | `true` | set `false` for plain-http LAN testing |
| `SESSION_DAYS` | `30` | |
| `MIN_INTERVAL_SECONDS` | `60` | floor for per-item interval |
| `CHECK_CONCURRENCY` | `4` | |
| `STATIC_DIR` | `/app/static` | built SPA |
| `TZ` | `UTC` | |
| `ENABLE_BROWSER` | `true` | allow Playwright fallback |

## Data model

- **User**: id, username (unique, case-insensitive), password_hash, is_admin, created_at
- **Session**: id (random token, stored hashed), user_id, created_at, expires_at, user_agent, ip
- **UserSettings** (1:1 user): ntfy_server (default `https://ntfy.sh`), ntfy_topic, ntfy_token (nullable, never returned in full — API returns `ntfy_token_set: bool`), ntfy_priority (1–5, default 4), default_interval_minutes (default 2), default_zip, default_max_distance_miles (default 25), notify_on_out_of_stock (default false), theme (`dark|light|system`)
- **Item**: id, user_id, name, url, kind (`generic|apple`), enabled, notify_enabled, interval_minutes, image_path, status (`in_stock|out_of_stock|unknown|error`), status_text, price, last_checked_at, last_change_at, last_error, consecutive_errors, available_keys (JSON list), last_result (JSON), generic_config (JSON), apple_config (JSON), created_at, updated_at
- **CheckEvent**: id, item_id, checked_at, status, status_text, error, duration_ms, changed (bool). Keep last 500 per item.
- **Notification**: id, user_id, item_id (nullable, SET NULL on delete), title, message, url, image_url, created_at, read, delivered, delivery_error

## REST API (all under `/api`, JSON, cookie-session auth)

Errors: `{"detail": "message"}` with proper status codes. Timestamps ISO-8601 UTC with `Z`.

Auth / session
- `GET  /health` (no auth) → `{status:"ok", version}`
- `GET  /auth/status` (no auth) → `{setup_required: bool, user: User|null}`
- `POST /auth/setup` `{username,password}` → `User` (only when zero users; creates admin, logs in)
- `POST /auth/login` `{username,password}` → `User` (sets `sw_session` cookie: HttpOnly, SameSite=Lax, Secure per config). Rate-limited (e.g. 10 fails / 15 min per IP+username → 429)
- `POST /auth/logout` → 204
- `GET  /auth/me` → `User`
- `POST /auth/change-password` `{current_password,new_password}` → 204 (min length 8)

`User` = `{id, username, is_admin, created_at}`

Users (admin only)
- `GET /users` → `User[]`
- `POST /users` `{username,password,is_admin}` → `User`
- `PATCH /users/{id}` `{password?, is_admin?}` → `User` (cannot remove last admin)
- `DELETE /users/{id}` → 204 (cannot delete self; cascades items/notifications)

Settings (per user)
- `GET /settings` → `Settings`
- `PUT /settings` (partial) → `Settings`. Sending `ntfy_token: ""` clears it; omitting keeps it.
- `POST /settings/test-notification` → `{ok: bool, error: string|null}`

`Settings` = `{ntfy_server, ntfy_topic, ntfy_token_set, ntfy_priority, default_interval_minutes, default_zip, default_max_distance_miles, notify_on_out_of_stock, theme}`

Items (scoped to current user)
- `GET /items` → `Item[]`
- `POST /items` `ItemCreate` → `Item` (201). Kicks off image fetch + first check in background
- `GET /items/{id}` → `Item`
- `PATCH /items/{id}` `ItemUpdate` (partial) → `Item`
- `DELETE /items/{id}` → 204
- `POST /items/{id}/check` → `Item` (runs a check now, awaits it, returns updated item)
- `POST /items/{id}/image` multipart `file` → `Item` (jpg/png/webp/gif ≤ 8 MB)
- `POST /items/{id}/image/refresh` → `Item` (re-fetch image from page)
- `GET /items/{id}/history?limit=50` → `CheckEvent[]` newest first
- `POST /items/preview` `{url}` → `Preview` (fetch page, auto-detect: name, image_url, price, status, is_apple)

`Item` =
```json
{
  "id": 1, "name": "iPhone 17 Pro 256GB", "url": "https://...", "kind": "generic|apple",
  "enabled": true, "notify_enabled": true, "interval_minutes": 2,
  "image_url": "/api/images/abc.jpg" ,
  "status": "in_stock|out_of_stock|unknown|error", "status_text": "Pickup at 2 stores",
  "price": "$1,099.00",
  "last_checked_at": "...Z", "last_change_at": "...Z", "last_error": null,
  "generic_config": {"mode":"auto|selector|text", "selector": null, "in_stock_text": null, "out_of_stock_text": null, "render_js": false},
  "apple_config": {"parts":[{"part_number":"MG8H4LL/A","label":"256GB Cosmic Orange"}], "zip":"95014", "max_distance_miles":25, "watch_pickup":true, "watch_delivery":true},
  "last_result": { },
  "created_at": "...Z", "updated_at": "...Z"
}
```
`ItemCreate` = `{name?, url, kind?, interval_minutes?, notify_enabled?, image_url? (remote url to download), generic_config?, apple_config?}` — `kind` defaults to `apple` if host is `*.apple.com` and apple_config given, else `generic`. `name` defaults from preview.

`last_result` for Apple items:
```json
{"stores":[{"store_number":"R014","name":"Valley Fair","city":"Santa Clara","distance_miles":3.2,
            "parts":[{"part_number":"MG8H4LL/A","label":"...","available":true,"quote":"Available Today"}]}],
 "delivery":[{"part_number":"MG8H4LL/A","label":"...","two_hour":true,"quote":"Delivers in 2 hours"}]}
```
`last_result` for generic items: `{"signals":["json-ld: InStock", ...], "matched": "..."}` (free-form detail for the UI's "why" line).

Apple helpers
- `POST /apple/resolve` `{url}` → `{product_name, image_url, variants:[{part_number, label, price}]}` (best effort; may return empty variants → UI falls back to manual part-number entry)

Notifications
- `GET /notifications?unread_only=false&limit=50&offset=0` → `{items: Notification[], unread_count, total}`
- `POST /notifications/{id}/read` → 204
- `POST /notifications/read-all` → 204
- `DELETE /notifications/{id}` → 204
- `DELETE /notifications` → 204 (clear all)

`Notification` = `{id, item_id, item_name, title, message, url, image_url, created_at, read, delivered, delivery_error}`

Stats
- `GET /stats` → `{total, in_stock, out_of_stock, unknown, error, paused, unread_notifications, checks_24h, alerts_24h}`

Images
- `GET /images/{filename}` → file (auth required; only files owned by the user's items)

## Checker contract (`backend/app/checkers/`)

`base.py` (written by supervisor, do not change the shapes):

```python
@dataclass
class Availability:
    key: str    # stable id: "stock" | "pickup:{store_number}:{part}" | "delivery2h:{part}"
    label: str  # human text, e.g. "Pickup today at Valley Fair (3.2 mi)"

@dataclass
class CheckResult:
    status: str                 # in_stock | out_of_stock | unknown | error
    status_text: str
    available: list[Availability]
    price: str | None = None
    title: str | None = None
    image_url: str | None = None
    detail: dict = field(default_factory=dict)   # stored as Item.last_result
    error: str | None = None
```

`__init__.py` exports (async):
- `run_check(kind, url, generic_config, apple_config) -> CheckResult`
- `preview_url(url) -> dict` → `{name, image_url, price, status, is_apple}`
- `resolve_apple(url) -> dict` → `/apple/resolve` payload
- `shutdown()` → close shared http client / browser

Checkers never raise for site problems — they return `status="error"` with `error` set.

## Scheduler & notification semantics

- Loop every 10 s: pick enabled items where `last_checked_at + interval <= now` (+ small jitter), run with `CHECK_CONCURRENCY` semaphore. Never run the same item twice concurrently.
- After a check: record `CheckEvent`; update item. On `status=error`, **keep previous `available_keys`** (no flapping alerts) and increment `consecutive_errors`.
- **Alert** = keys in new `available` that were not in previous `available_keys`. One notification per check summarizing all newly available keys (e.g. "iPhone 17 Pro: pickup available at Valley Fair, Stanford; 2-hour delivery available"). Created in the notification center and sent to ntfy (if topic configured and `notify_enabled`).
- Optional "back out of stock" notification when `notify_on_out_of_stock` and item went from in_stock → out_of_stock.
- ntfy: POST to `{server}/{topic}` with headers `Title`, `Priority`, `Tags` (`shopping_cart` / `apple`), `Click` (item url), `Attach`/`Icon` omitted unless public; `Authorization: Bearer {token}` if set. Record `delivered`/`delivery_error`.

## Security

- argon2id hashes; session tokens 32 random bytes, stored as SHA-256; cookie `sw_session`.
- CSRF: mutating requests require `Content-Type: application/json` or multipart **and** same-origin `Origin`/`Referer` when present; SameSite=Lax cookie.
- Uvicorn runs with `--proxy-headers --forwarded-allow-ips="*"` for reverse proxy.
- Every item/notification query filtered by `user_id`.

## Delivery / ops

- `Dockerfile`: multi-stage (node:22 build SPA → python:3.12-slim runtime with Playwright Chromium). Healthcheck on `/api/health`. Exposes 8000. Label `com.centurylinklabs.watchtower.enable=true`.
- `.github/workflows/docker.yml`: on push to `main` (+ manual), buildx multi-arch, push `latest` and `sha-<short>` to GHCR using `GITHUB_TOKEN`; run backend tests + frontend build first.
- `docker-compose.yml`: `stock-watcher` + `watchtower` (`WATCHTOWER_POLL_INTERVAL=300`, `WATCHTOWER_LABEL_ENABLE=true`, `WATCHTOWER_CLEANUP=true`, mounts `/var/run/docker.sock` and a `config.json` with GHCR creds for the private package).
- README: Synology setup (Container Manager project), GHCR PAT for Watchtower, reverse-proxy notes (WebSocket not needed), ntfy setup, first-run admin.

## Work split (agents)

| Agent | Model | Owns |
|---|---|---|
| Backend core | Sonnet | `backend/` except `backend/app/checkers/` |
| Checkers (generic + Apple) | Opus | `backend/app/checkers/`, `backend/tests/checkers/` |
| Frontend | Opus | `frontend/` |
| DevOps & docs | Haiku | `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `.github/`, `README.md` |
| Integration, review, E2E | Supervisor | everything |
