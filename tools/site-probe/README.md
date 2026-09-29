# Site probe

Stock Watcher checks product pages on 59 stores, and we can't reach most of those stores from our
test machines because the retailers block them. The site probe runs the app's own checker code on **your**
computer, from your home connection, and saves what each store sent back. You send us the results
and we fix the stores that fail.

## Quick start (about a minute)

You need Python 3.11 or newer ([python.org](https://www.python.org/downloads/); on macOS `brew install python@3.12` also works) and a copy of this repository.

**macOS / Linux**

```bash
cd stock-watcher/tools/site-probe
./run.sh serve                  # opens http://127.0.0.1:8765 in your browser
```

**Windows (PowerShell)**

```powershell
cd stock-watcher\tools\site-probe
powershell -ExecutionPolicy Bypass -File .\run.ps1 serve
```

The first run sets up `.venv/` in this folder, installs the backend's requirements and downloads Chromium
(for [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python), the Playwright build the app uses). That takes 1–2 minutes and only happens once. After that the wrapper passes your arguments
straight to `probe.py`.

In the page, paste product URLs or tick stores to use their sample URLs, then click **Run checks**. Results
show up as each check finishes. Click **Download bundle** when it's done.

### Command line

```bash
./run.sh check https://www.target.com/p/-/A-94693225 --zip 60601 --fulfillment pickup
./run.sh check URL1 URL2 --no-browser          # plain HTTP only, no browser
./run.sh sweep --only lego,bjs --chrome         # use your installed Google Chrome
./run.sh sweep --headless                       # no browser window
./run.sh sweep                                  # every store in sites.json (concurrency 3)
./run.sh sweep --only target,bestbuy,walmart
./run.sh bundle                                 # -> probe-results-YYYY-MM-DD.zip
```

`check` and `sweep` also accept `--radius 25`, `--store-id 151`, `--any-seller` (count marketplace sellers)
and `--browser/--no-browser`.

### Browser options

Many stores block headless browsers, so the probe runs a real, visible browser like the app does:

- `--headed` / `--headless`: window or no window. Default: headed on macOS and Windows; on Linux headed when
  there is a screen, else on a virtual display (Xvfb) if installed.
- `--chrome` / `--no-chrome`: your installed Google Chrome instead of the bundled Chromium. On macOS the
  default is Chrome when it is installed.
- `--cdp URL`: drive a Chrome you started yourself, so you can solve a captcha in it by hand:

  ```bash
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --remote-debugging-port=9222 \
      --user-data-dir="$HOME/.stockwatcher-chrome"
  ./run.sh sweep --cdp http://127.0.0.1:9222
  ```

The browser keeps its cookies in `.browser-profile/` here, so a store's "Just a moment…" check usually only
has to pass once. Delete that folder to start fresh. On Windows, use `.\run.ps1` wherever these examples use `./run.sh`.

`check` prints a summary for each URL: store, adapter used, status, price, seller, add-to-cart link, how the page
was fetched (http, curl or browser), duration, detection signals and a verdict:

| Verdict | Meaning |
|---|---|
| **OK** | The checker got a definite in-stock or out-of-stock answer. Compare it with what you see on the site. |
| **FAIL** | Error, or the page loaded but the checker couldn't tell. |
| **BLOCKED** | Bot protection (captcha, Akamai, PerimeterX, Cloudflare, …) stopped it. |
| **QUEUE** | A waiting room / queue page was shown. |

`sites.json` has **sample** URLs for every supported store. They were written without being able to open
the sites, so some may be stale. If one 404s or shows the wrong product, replace it with a current product
page. A mix of in-stock and sold-out items is most useful. An entry can be a plain URL or
`{"url": ..., "retailer_config": {"fulfillment": "pickup", "zip": "60601"}}`.

If you have store API keys, you can set them as environment variables before running and the probe will use
them: `BESTBUY_API_KEY`, `KROGER_CLIENT_ID`/`KROGER_CLIENT_SECRET`, `EBAY_CLIENT_ID`/`EBAY_CLIENT_SECRET`.
Their values are redacted from everything the probe saves.

## What gets recorded

Every check creates `probe-output/<timestamp>-<store>/` containing:

- `result.json`: the checker's full result (status, price, seller, availability, signals), the add-item
  preview, your options (ZIP, fulfillment, …), your OS/Python version and the browser used
  (`environment.browser`: engine, headed/headless/cdp, Chrome or Chromium and its version).
- `requests/index.json`: every request the checker made, with URL, status code, how it was fetched
  (`http`/`curl`/`browser`), response headers and timing.
- `requests/NNN-<host>.html|json|txt`: the response bodies, up to 2 MB each.

`sweep` also writes `probe-output/report.md` (the browser engine and mode in its header, then a table: store, URL,
status, adapter, price, verdict, error) and `report.json`.

## Privacy

- Everything runs locally. Requests go from your computer, over your home IP address, straight to the stores,
  exactly as the app would send them. The probe has no telemetry and never uploads anything.
- It does not use your browser profile, logins or cookies. It keeps its own profile in `.browser-profile/`
  (cookies the stores set, nothing else, never bundled). With `--cdp` it uses the Chrome you started, with
  whatever that Chrome is logged into: start it with its own `--user-data-dir` as shown above.
- Cookie, Set-Cookie, Authorization and similar headers are replaced with `[stripped]` when saved, and
  again when bundled. Store API keys from the environment are redacted.
- The recordings are product pages as the store served them. They can include your approximate
  location if a store guesses it from your IP (for example "your store: Oak Park"), and the ZIP you entered.
  Open `probe-output/` before sharing if you want to check.
- The web UI listens on `127.0.0.1` only, so other devices on your network can't reach it.
- **Nothing leaves your machine unless you share the zip yourself.**

## Sending results back

1. Run `./run.sh bundle` (or use **Download bundle** in the web UI). That creates
   `probe-results-YYYY-MM-DD.zip`, capped at 25 MB (`--max-mb` to change). If the cap is hit, the largest
   page bodies are left out and listed in `MANIFEST.txt`.
2. Attach the zip in the chat with us or to a GitHub issue.
   If attaching isn't possible, paste `probe-output/report.md` instead. The table alone already tells us
   which stores are broken.

## For maintainers: recordings → regression tests

```bash
cd tools/site-probe
python probe.py fixture probe-output/20260929-181502-target --name target_in_stock
python probe.py fixture ~/Downloads/probe-results-2026-09-29.zip --entry 20260929-181502-target --name target_in_stock
```

This copies the recorded bodies to `backend/tests/checkers/fixtures/live/<store>/<name>__NN.<ext>` and adds
an entry to that folder's `index.json`: the source URL, capture date, options, each file's request
URL/status/via, and the observed result (status, price, adapter, verdict). Use `--force` to replace an entry.
A test can then serve those files with `respx` for the recorded URLs and assert the adapter returns
the observed status, or the correct one if the probe caught a bug. Check the observed result against the live
site before treating it as the expected answer.

The probe reads the backend's `fetcher.recording()` hook, which records every HTTP, curl and browser fetch.
On an older backend without the hook it falls back to recording plain httpx traffic only (browser fetches are
missing) and prints a note saying so.

Tests (no network):

```bash
cd backend && python -m pytest ../tools/site-probe -q
```
