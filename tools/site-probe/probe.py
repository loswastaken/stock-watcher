#!/usr/bin/env python3
"""Stock Watcher site probe: run the production checkers against real store pages from your
own computer and save everything they fetched, so the maintainers can see what each site
really returns.

    python probe.py check URL [URL ...] [--zip 60601 --fulfillment pickup ...]
    python probe.py sweep [--only target,bestbuy] [--sites sites.json] [--discover]
    python probe.py discover [--only target,bestbuy] [--per-store 2] [--write]
    python probe.py bundle
    python probe.py serve [--port 8765]
    python probe.py fixture <bundle-dir|zip> --name target_in_stock

It imports ../../backend so the exact checker code the app runs is what gets tested.
Nothing is sent anywhere: results stay in probe-output/ until you share the zip yourself.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import contextvars
import dataclasses
import datetime as dt
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
import zipfile
import zlib
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

HERE = Path(__file__).resolve().parent
BACKEND = Path(os.environ.get("STOCK_WATCHER_BACKEND") or HERE.parent.parent / "backend").resolve()
DEFAULT_OUT = HERE / "probe-output"
DEFAULT_SITES = HERE / "sites.json"
# `discover --write` saves here (git-ignored) so `git pull` never conflicts with it; when it
# exists, sweep/discover/serve read it instead of the shipped sites.json.
LOCAL_SITES = HERE / "sites.local.json"


def effective_sites(path: Path | None) -> Path:
    if path is not None:
        return Path(path)
    return LOCAL_SITES if LOCAL_SITES.exists() else DEFAULT_SITES
# The probe's own persistent browser profile (store cookies survive between runs).
DEFAULT_PROFILE = HERE / ".browser-profile"
LIVE_FIXTURES = BACKEND / "tests" / "checkers" / "fixtures" / "live"
PROBE_VERSION = 1
MAX_BODY = 2_000_000
DEFAULT_CAP_MB = 25
VERDICTS = ("OK", "INFO", "STALE", "FAIL", "BLOCKED", "QUEUE")
# verdicts that are not a failure of the store check (``check`` exits 0 on these)
PASSING = frozenset({"OK", "INFO"})

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


class ProbeError(Exception):
    """A user-facing problem (bad arguments, missing backend, ...)."""


# --------------------------------------------------------------------------- backend access


def backend() -> tuple[Any, Any, Any]:
    """(app.checkers, app.checkers.fetcher, registry module), with a clear error if unavailable."""
    try:
        import app.checkers as checkers
        from app.checkers import fetcher
        from app.checkers.retailers import registry
    except ImportError as e:
        raise ProbeError(
            f"Could not import the Stock Watcher backend from {BACKEND} ({e}).\n"
            "Run the probe through run.sh / run.ps1 (they install backend/requirements.txt), "
            "or set STOCK_WATCHER_BACKEND to the backend folder."
        ) from e
    return checkers, fetcher, registry


def playwright_available() -> bool:
    """patchright or playwright is importable."""
    import importlib.util

    return any(importlib.util.find_spec(n) is not None for n in ("patchright", "playwright"))


def browser_info() -> dict:
    """The backend's browser engine / mode (``fetcher.browser_info()``), {} on an older backend."""
    with contextlib.suppress(Exception):
        _, fetcher, _ = backend()
        fn = getattr(fetcher, "browser_info", None)
        if callable(fn):
            return dict(fn())
    return {}


def describe_browser(info: dict) -> str:
    """One line for the report header, e.g. "patchright, headed-xvfb, chrome 141.0.7390.37, persistent profile"."""
    if not info:
        return "unknown (older backend)"
    parts = [str(info.get("engine") or "no engine installed"), str(info.get("mode") or "?")]
    if info.get("mode") != "cdp":
        parts.append(" ".join(str(x) for x in (info.get("channel") or "chromium", info.get("version")) if x))
        parts.append("persistent profile" if info.get("persistent_profile") else "temporary profile")
    elif info.get("version"):
        parts.append(f"Chrome {info['version']}")
    if not info.get("launched"):
        parts.append("not started")
    return ", ".join(p for p in parts if p)


# --------------------------------------------------------------------------- scrubbing

SENSITIVE_HEADERS = {
    "cookie", "set-cookie", "set-cookie2", "authorization", "proxy-authorization",
    "x-api-key", "api-key", "x-auth-token", "x-csrf-token", "x-xsrf-token",
}
SECRET_QUERY_PARAMS = {"apikey", "api_key", "access_token", "client_secret", "token", "password"}
SECRET_ENV = ("BESTBUY_API_KEY", "KROGER_CLIENT_ID", "KROGER_CLIENT_SECRET", "EBAY_CLIENT_ID",
              "EBAY_CLIENT_SECRET", "STOCK_WATCHER_SECRET")
STRIPPED = "[stripped]"
REDACTED = "[redacted]"


def scrub_headers(headers: Any) -> dict[str, str]:
    """Headers as a dict with cookies / auth values replaced by ``[stripped]``."""
    if not headers:
        return {}
    items = headers.items() if hasattr(headers, "items") else headers
    out: dict[str, str] = {}
    for pair in items:
        try:
            k, v = pair
        except (TypeError, ValueError):
            continue
        k = str(k)
        if k.lower() in SENSITIVE_HEADERS:
            out[k] = STRIPPED
        else:
            out[k] = scrub_text(str(v))
    return out


def _secret_values() -> list[str]:
    return [v for v in (os.environ.get(n, "").strip() for n in SECRET_ENV) if len(v) >= 6]


def scrub_text(text: str) -> str:
    """Remove the values of known secret environment variables (store API keys) from text."""
    for secret in _secret_values():
        if secret in text:
            text = text.replace(secret, REDACTED)
    return text


def scrub_url(url: str) -> str:
    url = scrub_text(url or "")
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    q = parse_qsl(parts.query, keep_blank_values=True)
    if not any(k.lower() in SECRET_QUERY_PARAMS for k, _ in q):
        return url
    q = [(k, REDACTED if k.lower() in SECRET_QUERY_PARAMS else v) for k, v in q]
    return urlunsplit(parts._replace(query=urlencode(q, safe="[]")))


def scrub_entry(entry: dict) -> dict:
    """A recorded request (without its body) made safe to share."""
    out: dict[str, Any] = {}
    for k, v in entry.items():
        if k == "body":
            continue
        lk = k.lower()
        if "header" in lk and isinstance(v, (dict, list, tuple)):
            out[k] = scrub_headers(v)
        elif lk.endswith("url") and isinstance(v, str):
            out[k] = scrub_url(v)
        elif isinstance(v, str):
            out[k] = scrub_text(v)
        else:
            out[k] = v
    return out


def normalize_entry(raw: Any) -> dict:
    """Accept whatever the recording hook produced and return the documented shape (+ body)."""
    e = dict(raw) if isinstance(raw, dict) else {"error": f"unrecognised record: {type(raw).__name__}"}
    body = e.get("body")
    if isinstance(body, (bytes, bytearray)):
        body = bytes(body).decode("utf-8", "replace")
    body = "" if body is None else str(body)
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY]
    e["body"] = body
    e.setdefault("method", "GET")
    e.setdefault("url", "")
    e.setdefault("status", None)
    e.setdefault("via", "http")
    e.setdefault("headers", {})
    e.setdefault("elapsed_ms", None)
    return e


# --------------------------------------------------------------------------- fetch recording

_fallback_log: contextvars.ContextVar[list | None] = contextvars.ContextVar("site_probe_log", default=None)
_fallback_installed = False
_warned_no_hook = False
HOOK_MISSING_MSG = (
    "NOTE: this backend has no fetcher.recording() hook yet, so the probe records plain HTTP (httpx) "
    "traffic only; pages fetched through the headless browser are not saved. Pull the latest "
    "stock-watcher code to record everything."
)


def _install_httpx_fallback() -> None:
    """Record httpx traffic by wrapping AsyncClient.send (only while a probe log is active)."""
    global _fallback_installed
    if _fallback_installed:
        return
    import httpx

    orig_send = httpx.AsyncClient.send

    async def send(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        log = _fallback_log.get()
        t0 = time.monotonic()
        try:
            resp = await orig_send(self, request, *args, **kwargs)
        except Exception as e:
            if log is not None:
                log.append({"method": request.method, "url": str(request.url), "status": None, "via": "http",
                            "headers": {}, "request_headers": dict(request.headers), "body": "",
                            "elapsed_ms": round((time.monotonic() - t0) * 1000), "error": f"{type(e).__name__}: {e}"})
            raise
        if log is not None:
            try:
                await resp.aread()
                body = resp.text
            except Exception:  # noqa: BLE001
                body = ""
            log.append({"method": request.method, "url": str(resp.url), "status": resp.status_code, "via": "http",
                        "headers": dict(resp.headers), "request_headers": dict(request.headers),
                        "body": body[:MAX_BODY], "elapsed_ms": round((time.monotonic() - t0) * 1000)})
        return resp

    httpx.AsyncClient.send = send  # type: ignore[method-assign]
    _fallback_installed = True


@contextlib.asynccontextmanager
async def record_fetches(fetcher: Any):
    """Yield ``(log, mode)``: every fetch made inside is appended to ``log``.

    Uses the backend's ``fetcher.recording()`` hook when present; otherwise falls back to
    recording httpx traffic only (and says so once)."""
    global _warned_no_hook
    hook = getattr(fetcher, "recording", None)
    if callable(hook):
        cm = hook()
        if hasattr(cm, "__aenter__"):
            async with cm as log:
                yield (log if log is not None else []), "fetcher.recording"
        else:
            with cm as log:
                yield (log if log is not None else []), "fetcher.recording"
        return
    if not _warned_no_hook:
        _warned_no_hook = True
        print(HOOK_MISSING_MSG, file=sys.stderr)
    _install_httpx_fallback()
    log: list = []
    token = _fallback_log.set(log)
    try:
        yield log, "httpx-fallback"
    finally:
        _fallback_log.reset(token)


# --------------------------------------------------------------------------- verdicts

_BLOCK_RE = re.compile(
    r"bot protection|blocked|captcha|access denied|robot|challenge|HTTP (?:403|429)\b|perimeterx|akamai"
    r"|pardon our interruption|areyouahuman",
    re.I,
)


# The link itself is dead or points elsewhere: the store answered (often through the browser, after a
# challenge page or a 403 on the way), so this is never bot protection.
_STALE_RE = re.compile(
    r"listing not found|product page not found|page not found|link now shows a different product"
    r"|redirects? (?:this item )?to (?:the homepage|a non-product page|a different product)|link may be stale"
    r"|update the link|no longer exists",
    re.I,
)
# Pages that don't sell anything themselves (brand spec sheets, NVIDIA's www.nvidia.com GPU pages): nothing
# failed, there is just no stock to watch there.
_INFO_RE = re.compile(r"^\s*(?:no direct sales on this page|info page\b)", re.I)


def classify(status: str, status_text: str | None, error: str | None, detail: dict,
             entries: list[dict] | None = None, looks_like_challenge: Callable[[str], bool] | None = None
             ) -> tuple[str, str]:
    """→ (verdict, reason). OK = the checker reached a definite in/out answer; INFO = an info page with no
    direct sales (not a failure); STALE = the link is dead / shows another product (a failure: update the
    link) — decided before BLOCKED, whatever 403s or challenge pages were met on the way."""
    detail = detail or {}
    if detail.get("queue"):
        return "QUEUE", status_text or "Waiting room active"
    if status in ("in_stock", "out_of_stock"):
        return "OK", status_text or status
    msg = error or status_text or ""
    if status != "error" and _INFO_RE.search(status_text or ""):
        return "INFO", status_text or "Info page"
    if detail.get("dead_link") or _STALE_RE.search(msg) or _STALE_RE.search(status_text or ""):
        return "STALE", msg or status_text or "Stale link"
    if _BLOCK_RE.search(msg):
        return "BLOCKED", msg
    for e in entries or []:
        st = e.get("status")
        body = e.get("body") or ""
        if st in (403, 429) or (looks_like_challenge and body and looks_like_challenge(body)):
            why = f"HTTP {st}" if st in (403, 429) else "bot challenge page"
            return "BLOCKED", f"{why} from {urlsplit(e.get('url') or '').hostname or '?'}" + (f" ({msg})" if msg else "")
    if status == "error":
        return "FAIL", msg or "Check failed"
    return "FAIL", f"Could not decide: {status_text or 'Unknown'}"


def retired_verdict(retailer: Any, verdict: str, reason: str) -> tuple[str, str]:
    """A dead link on a store the registry marks ``retired`` (it no longer sells online) is expected: INFO,
    not STALE — there is no current link to update it to."""
    why = getattr(retailer, "retired", None) if retailer is not None else None
    if why and (verdict in ("STALE", "FAIL") or (verdict == "OK" and getattr(retailer, "retired_unrelated", False))):
        # (an OK on a domain that now runs an unrelated store — acegraphicscards.com's handbags — is as meaningless)
        return "INFO", f"Store no longer sells online — {why}"
    return verdict, reason


# --------------------------------------------------------------------------- one check


def _jsonable(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=str))


def _ext_for(entry: dict) -> str:
    ctype = ""
    for k, v in (entry.get("headers") or {}).items():
        if str(k).lower() == "content-type":
            ctype = str(v).lower()
    body = (entry.get("body") or "").lstrip()[:1]
    if "json" in ctype:
        return ".json"
    if "html" in ctype:
        return ".html"
    if "xml" in ctype:
        return ".xml"
    if "javascript" in ctype:
        return ".js"
    if body in ("{", "["):
        return ".json"
    if body == "<":
        return ".html"
    return ".txt"


def _slug(s: str, n: int = 40) -> str:
    return (re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "site")[:n]


def _unique_dir(root: Path, name: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for i in range(1, 1000):
        cand = root / (name if i == 1 else f"{name}-{i}")
        try:
            cand.mkdir()
            return cand
        except FileExistsError:
            continue
    raise ProbeError(f"Could not create an output folder under {root}")


def save_bundle(out_root: Path, summary: dict, result: dict, preview: dict | None, entries: list[dict],
                meta: dict | None = None) -> Path:
    """Write ``<out_root>/<timestamp>-<retailer>/`` with result.json + requests/ bodies and index.json."""
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = summary.get("retailer") or _slug(urlsplit(summary.get("url") or "").hostname or "site")
    d = _unique_dir(Path(out_root), f"{stamp}-{name}")
    req_dir = d / "requests"
    req_dir.mkdir()
    index = []
    for i, raw in enumerate(entries, 1):
        e = normalize_entry(raw)
        rec = scrub_entry(e)
        host = _slug(urlsplit(e.get("url") or "").hostname or "unknown", 30)
        if e["body"]:
            fname = f"{i:03d}-{host}{_ext_for(e)}"
            (req_dir / fname).write_text(scrub_text(e["body"]), encoding="utf-8")
            rec["body_file"] = f"requests/{fname}"
            rec["body_bytes"] = len(e["body"].encode("utf-8"))
        else:
            rec["body_file"] = None
        rec["n"] = i
        index.append(rec)
    (req_dir / "index.json").write_text(json.dumps(_jsonable(index), indent=2), encoding="utf-8")
    summary["bundle"] = d.name
    doc = {
        "probe_version": PROBE_VERSION,
        "summary": summary,
        "result": result,
        "preview": preview,
        **(meta or {}),
    }
    doc = json.loads(scrub_text(json.dumps(_jsonable(doc))))
    (d / "result.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return d


def environment_info() -> dict:
    return {
        "os": f"{platform.system()} {platform.release()}",
        "machine": platform.machine(),
        "python": platform.python_version(),
        "browser_enabled": os.environ.get("ENABLE_BROWSER", "true"),
        "playwright_installed": playwright_available(),
        "browser": browser_info(),
    }


async def probe_one(url: str, retailer_config: dict | None = None, generic_config: dict | None = None, *,
                    preview: bool = True, out_root: Path | None = DEFAULT_OUT) -> dict:
    """Run one check (and optional preview) under recording; save a bundle; return a summary dict."""
    checkers, fetcher, registry = backend()
    url = url.strip()
    retailer = registry.match_retailer(url)
    started = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    t0 = time.monotonic()
    async with record_fetches(fetcher) as (log, mode):
        res = await checkers.run_check("generic", url, generic_config, None, retailer_config)
        check_entries = [dict(normalize_entry(e), phase="check") for e in list(log)]
    duration = time.monotonic() - t0
    preview_out = None
    preview_entries: list[dict] = []
    if preview:
        async with record_fetches(fetcher) as (plog, _):
            preview_out = await checkers.preview_url(url)
            preview_entries = [dict(normalize_entry(e), phase="preview") for e in list(plog)]
    result = _jsonable(dataclasses.asdict(res) if dataclasses.is_dataclass(res) else dict(res))
    detail = result.get("detail") or {}
    entries = check_entries + preview_entries
    vias = [e.get("via") for e in check_entries if e.get("via")]
    fetched_via = detail.get("fetched_via") or ("browser" if "browser" in vias else (vias[-1] if vias else None))
    verdict, reason = classify(result.get("status") or "unknown", result.get("status_text"), result.get("error"),
                               detail, check_entries, getattr(fetcher, "looks_like_challenge", None))
    verdict, reason = retired_verdict(retailer, verdict, reason)
    expected = retailer.adapter.partition(":")[0] if retailer and retailer.adapter else "generic"
    if retailer and retailer.adapter:
        with contextlib.suppress(Exception):
            from app.checkers.retailers import load_adapter

            if load_adapter(retailer.adapter) is None:
                expected = f"generic (adapter {retailer.adapter} missing)"
    # An adapter that raised (site error) leaves no detail.adapter; don't report that as "generic".
    adapter = detail.get("adapter") or (expected if result.get("status") == "error" and expected[:7] != "generic"
                                        else "generic")
    summary = {
        "url": url,
        "retailer": retailer.key if retailer else None,
        "retailer_name": retailer.name if retailer else (urlsplit(url).hostname or url),
        "expected_adapter": expected,
        "adapter": adapter,
        "status": result.get("status"),
        "status_text": result.get("status_text"),
        "price": result.get("price"),
        "title": result.get("title"),
        "seller": detail.get("seller"),
        "third_party": detail.get("third_party"),
        "cart_url": detail.get("cart_url"),
        "fetched_via": fetched_via,
        "available": [a.get("label") or a.get("key") for a in result.get("available") or []],
        "signals": list(detail.get("signals") or [])[:15],
        "error": result.get("error"),
        "queue": bool(detail.get("queue")),
        "verdict": verdict,
        "verdict_reason": reason,
        "duration_s": round(duration, 2),
        "requests": len(check_entries),
        "recording": mode,
        "started_at": started,
        "retailer_config": retailer_config or {},
    }
    if preview_out is not None:
        summary["preview"] = {k: preview_out.get(k) for k in ("name", "price", "status", "image_url", "error")}
    if out_root is not None:
        d = save_bundle(Path(out_root), summary, result, preview_out, entries,
                        meta={"retailer_config": retailer_config, "generic_config": generic_config,
                              "environment": environment_info()})
        summary["bundle_path"] = str(d)
    return summary


def error_summary(entry: dict, exc: BaseException) -> dict:
    return {
        "url": entry.get("url"), "retailer": entry.get("key"), "retailer_name": entry.get("key"),
        "adapter": None, "status": "error", "status_text": "Probe crashed", "price": None,
        "error": f"{type(exc).__name__}: {exc}", "verdict": "FAIL", "verdict_reason": f"probe crashed: {exc}",
        "signals": [], "available": [], "requests": 0, "duration_s": None,
    }


def format_summary(s: dict) -> str:
    rows = [f"{s.get('retailer_name')} ({s.get('retailer') or 'not a registered store'})  {s.get('url')}"]

    def add(label: str, value: Any) -> None:
        if value not in (None, "", [], {}):
            rows.append(f"  {label:<12}{value}")

    exp = s.get("expected_adapter")
    add("adapter", f"{s.get('adapter')}" + (f"  (registry expects {exp})" if exp and exp != s.get("adapter") else ""))
    add("status", f"{s.get('status')} - {s.get('status_text')}")
    add("price", s.get("price"))
    add("title", s.get("title"))
    seller = s.get("seller")
    if seller or s.get("third_party") is not None:
        add("seller", f"{seller or '?'}" + (" (third party)" if s.get("third_party") else ""))
    add("cart_url", s.get("cart_url"))
    add("available", "; ".join(x for x in s.get("available") or [] if x))
    add("fetched via", s.get("fetched_via"))
    add("duration", f"{s.get('duration_s')}s, {s.get('requests')} request(s) recorded via {s.get('recording')}")
    for sig in (s.get("signals") or [])[:8]:
        add("signal", sig)
    add("error", s.get("error"))
    pv = s.get("preview")
    if pv:
        add("preview", f"name={pv.get('name')!r} price={pv.get('price')!r} status={pv.get('status')}"
            + (f" error={pv.get('error')}" if pv.get("error") else ""))
    add("VERDICT", f"{s.get('verdict')} - {s.get('verdict_reason')}")
    add("saved to", _rel(s.get("bundle_path")))
    return "\n".join(rows)


def _rel(p: Any) -> Any:
    if not p:
        return p
    with contextlib.suppress(ValueError):
        rel = os.path.relpath(p)
        if not rel.startswith(".."):
            return rel
    return str(p)


# --------------------------------------------------------------------------- sites + sweep


def load_sites(path: Path = DEFAULT_SITES) -> dict[str, list[dict]]:
    """→ {retailer_key: [{url, retailer_config, note}]}"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ProbeError(f"sites file not found: {path}") from e
    except json.JSONDecodeError as e:
        raise ProbeError(f"{path} is not valid JSON: {e}") from e
    return load_sites_data(data)


def load_sites_data(data: Any) -> dict[str, list[dict]]:
    """``load_sites`` for an already-parsed sites document."""
    sites = data.get("sites", data) if isinstance(data, dict) else {}
    out: dict[str, list[dict]] = {}
    for key, items in sites.items():
        if key.startswith("_"):
            continue
        items = items if isinstance(items, list) else [items]
        norm = []
        for it in items:
            if isinstance(it, str):
                norm.append({"key": key, "url": it, "retailer_config": {}, "note": None})
            elif isinstance(it, dict) and it.get("url"):
                norm.append({"key": key, "url": it["url"], "retailer_config": dict(it.get("retailer_config") or {}),
                             "note": it.get("note")})
        out[key] = norm
    return out


def select_entries(sites: dict[str, list[dict]], only: list[str] | None) -> list[dict]:
    if not only:
        return [e for items in sites.values() for e in items]
    unknown = [k for k in only if k not in sites]
    if unknown:
        raise ProbeError(f"No sample URLs for: {', '.join(unknown)}. Known stores: {', '.join(sorted(sites))}")
    return [e for k in only for e in sites[k]]


async def run_entries(entries: list[dict], *, concurrency: int = 3, preview: bool = False,
                      base_config: dict | None = None, generic_config: dict | None = None,
                      out_root: Path | None = DEFAULT_OUT,
                      on_result: Callable[[int, dict], None] | None = None) -> list[dict]:
    sem = asyncio.Semaphore(max(1, concurrency))
    results: list[dict | None] = [None] * len(entries)

    async def one(i: int, e: dict) -> None:
        async with sem:
            rc = {**(base_config or {}), **(e.get("retailer_config") or {})}
            try:
                s = await probe_one(e["url"], rc or None, generic_config, preview=preview, out_root=out_root)
            except asyncio.CancelledError:
                raise
            except Exception as ex:  # noqa: BLE001 - one bad site must not stop the sweep
                s = error_summary(e, ex)
            s["store"] = e.get("key") or s.get("retailer")
            if e.get("note"):
                s["note"] = e["note"]
            results[i] = s
            if on_result:
                on_result(i, s)

    await asyncio.gather(*(one(i, e) for i, e in enumerate(entries)))
    return [r for r in results if r is not None]


def _md(s: Any, n: int = 160) -> str:
    s = "" if s is None else str(s)
    s = s.replace("|", "\\|").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def sites_label(path: Path | None) -> str | None:
    """How the report names the sample file a sweep used (sites.local.json = `discover --write` output)."""
    if path is None:
        return None
    p = Path(path)
    if p.name == LOCAL_SITES.name:
        return f"{p.name} (from `discover --write`)"
    if p.name == DEFAULT_SITES.name:
        return f"{p.name} (shipped samples)"
    return p.name


def write_report(results: list[dict], out_root: Path = DEFAULT_OUT, *, title: str = "Site probe report",
                 sites: Path | str | None = None) -> tuple[Path, Path]:
    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    counts = {v: sum(1 for r in results if r.get("verdict") == v) for v in VERDICTS}
    env = environment_info()
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    lines = [
        f"# {title}",
        "",
        f"Generated {now} on {env['os']} ({env['machine']}), Python {env['python']}, "
        f"browser {'on' if env['browser_enabled'].lower() not in ('0', 'false', 'no', 'off') else 'off'}"
        f" (playwright {'installed' if env['playwright_installed'] else 'missing'}).",
        "",
        f"Browser engine: {describe_browser(env.get('browser') or {})}.",
        "",
        *([f"Sample URLs from: {sites if isinstance(sites, str) else sites_label(sites)}.", ""] if sites else []),
        f"**{len(results)} checks:** " + ", ".join(f"{counts[v]} {v}" for v in VERDICTS),
        "",
        "Verdicts: OK = definite in/out-of-stock answer; INFO = info page, no direct sales (not a failure); "
        "STALE = stale link (404, delisted, redirects away or shows another product — update it); "
        "FAIL = error or could not decide; BLOCKED = bot protection / captcha; QUEUE = waiting room.",
        "",
        "| Store | URL | Status | Adapter | Price | Verdict | Error |",
        "|---|---|---|---|---|---|---|",
    ]
    order = {"BLOCKED": 0, "FAIL": 1, "STALE": 2, "QUEUE": 3, "INFO": 4, "OK": 5}
    ordered = sorted(results, key=lambda r: (order.get(r.get("verdict") or "", 9), r.get("store") or r.get("retailer") or ""))
    for r in ordered:
        status = r.get("status") or ""
        if r.get("status_text") and r.get("status_text") != status:
            status = f"{status} ({r.get('status_text')})"
        err = r.get("error") or ("" if r.get("verdict") == "OK" else r.get("verdict_reason"))
        lines.append("| " + " | ".join([
            _md(r.get("store") or r.get("retailer") or r.get("retailer_name")),
            _md(r.get("url"), 90),
            _md(status, 60),
            _md(r.get("adapter")),
            _md(r.get("price")),
            f"**{r.get('verdict')}**",
            _md(err, 140),
        ]) + " |")
    attention = [r for r in ordered if r.get("verdict") != "OK"]
    if attention:
        lines += ["", "## Details for non-OK checks", ""]
        for r in attention:
            lines.append(f"### {r.get('store') or r.get('retailer')} — {r.get('verdict')}")
            lines.append(f"- URL: {r.get('url')}")
            lines.append(f"- Adapter: {r.get('adapter')} (registry: {r.get('expected_adapter')}), "
                         f"fetched via {r.get('fetched_via')}, {r.get('requests')} request(s), {r.get('duration_s')}s")
            lines.append(f"- Result: {r.get('status')} — {r.get('status_text')}")
            if r.get("error"):
                lines.append(f"- Error: {_md(r.get('error'), 400)}")
            for sig in (r.get("signals") or [])[:6]:
                lines.append(f"- Signal: {_md(sig, 200)}")
            if r.get("bundle"):
                lines.append(f"- Recording: `{r.get('bundle')}/`")
            lines.append("")
    lines += ["", "---", "Send this back: run `probe bundle` and attach the zip, or paste this file into the chat / a GitHub issue."]
    md = out_root / "report.md"
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    js = out_root / "report.json"
    shareable = [{k: v for k, v in r.items() if k != "bundle_path"} for r in results]  # no local paths
    sites_name = (sites if isinstance(sites, str) else sites_label(sites)) if sites else None
    js.write_text(json.dumps(_jsonable({"generated_at": now, "environment": env, "sites": sites_name,
                                        "counts": counts, "results": shareable}), indent=2), encoding="utf-8")
    return md, js


def format_progress(i: int, total: int, s: dict) -> str:
    w = len(str(total))
    return (f"[{i:>{w}}/{total}] {s.get('verdict', '?'):<7} {str(s.get('store') or s.get('retailer') or '-'):<14} "
            f"{str(s.get('status') or ''):<12} {str(s.get('price') or ''):<10} {s.get('url')}")


# --------------------------------------------------------------------------- bundle (zip)

_JSON_TO_RESCRUB = {"index.json", "result.json", "report.json"}


def _rescrub_json(data: Any) -> Any:
    if isinstance(data, list):
        return [_rescrub_json(x) for x in data]
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if "header" in str(k).lower() and isinstance(v, (dict, list)):
                out[k] = scrub_headers(v)
            elif str(k).lower().endswith("url") and isinstance(v, str):
                out[k] = scrub_url(v)
            else:
                out[k] = _rescrub_json(v)
        return out
    if isinstance(data, str):
        return scrub_text(data)
    return data


def make_bundle(out_root: Path = DEFAULT_OUT, dest_dir: Path = HERE, cap_mb: float = DEFAULT_CAP_MB,
                date: dt.date | None = None) -> tuple[Path, list[str], list[str]]:
    """Zip probe-output/ (scrubbed again, largest bodies dropped first past the cap) → (zip, included, skipped)."""
    out_root = Path(out_root)
    if not out_root.is_dir() or not any(out_root.iterdir()):
        raise ProbeError(f"Nothing to bundle: {out_root} is empty. Run `check` or `sweep` first.")
    cap = int(cap_mb * 1024 * 1024)
    date = date or dt.date.today()
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    zpath = dest_dir / f"probe-results-{date:%Y-%m-%d}.zip"
    files = [p for p in out_root.rglob("*") if p.is_file() and p.suffix != ".zip"]

    def tier(p: Path) -> tuple[int, int, str]:
        rel = p.relative_to(out_root).as_posix()
        if p.name in ("report.md", "report.json"):
            return (0, 0, rel)
        if p.name in _JSON_TO_RESCRUB:
            return (1, 0, rel)
        return (2, p.stat().st_size, rel)  # bodies: smallest first so more sites fit

    included: list[str] = []
    skipped: list[str] = []
    total = 0
    tmp = zpath.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(files, key=tier):
            rel = p.relative_to(out_root).as_posix()
            data = p.read_bytes()
            if p.name in _JSON_TO_RESCRUB:
                with contextlib.suppress(ValueError, UnicodeDecodeError):
                    data = json.dumps(_rescrub_json(json.loads(data.decode("utf-8"))), indent=2).encode("utf-8")
            elif _secret_values():
                data = scrub_text(data.decode("utf-8", "replace")).encode("utf-8")
            est = len(zlib.compress(data, 6)) + 200
            if total + est > cap and tier(p)[0] >= 1:
                skipped.append(rel)
                continue
            zf.writestr(f"probe-output/{rel}", data)
            total += est
            included.append(rel)
        manifest = [
            f"Stock Watcher site probe results, {dt.datetime.now().astimezone().isoformat(timespec='seconds')}",
            f"Size cap: {cap_mb} MB. Included {len(included)} file(s), skipped {len(skipped)}.",
            "Cookie / Set-Cookie / Authorization headers and store API keys were stripped.",
            "",
        ]
        if skipped:
            manifest += ["Skipped (over the size cap; rerun `bundle --max-mb N` to include):"] + [f"  {s}" for s in skipped]
        zf.writestr("MANIFEST.txt", "\n".join(manifest) + "\n")
    os.replace(tmp, zpath)
    return zpath, included, skipped


# --------------------------------------------------------------------------- fixtures

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")


def _find_bundle_dirs(root: Path) -> list[Path]:
    return sorted(p.parent for p in root.rglob("result.json"))


def make_fixture(src: Path, name: str, dest_root: Path = LIVE_FIXTURES, entry: str | None = None,
                 force: bool = False) -> Path:
    """Copy a check bundle's recorded bodies into fixtures/live/<retailer>/ and update its index.json."""
    if not _NAME_RE.match(name):
        raise ProbeError("--name must be lowercase letters, digits and underscores (e.g. target_in_stock)")
    src = Path(src)
    with contextlib.ExitStack() as stack:
        if src.is_file() and src.suffix == ".zip":
            tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            with zipfile.ZipFile(src) as zf:
                for m in zf.namelist():  # refuse path traversal
                    target = (tmp / m).resolve()
                    if not str(target).startswith(str(tmp.resolve())):
                        raise ProbeError(f"Unsafe path in zip: {m}")
                zf.extractall(tmp)
            dirs = _find_bundle_dirs(tmp)
            if entry:
                dirs = [d for d in dirs if d.name == entry]
            if len(dirs) != 1:
                names = "\n  ".join(d.name for d in _find_bundle_dirs(tmp)) or "(none)"
                raise ProbeError(f"Pick one check with --entry. Checks in {src.name}:\n  {names}")
            bdir = dirs[0]
        elif src.is_dir() and (src / "result.json").is_file():
            bdir = src
        else:
            raise ProbeError(f"{src} is not a check folder (with result.json) or a probe-results zip")
        doc = json.loads((bdir / "result.json").read_text(encoding="utf-8"))
        idx_path = bdir / "requests" / "index.json"
        index = json.loads(idx_path.read_text(encoding="utf-8")) if idx_path.is_file() else []
        summary = doc.get("summary") or {}
        retailer = summary.get("retailer") or "unknown"
        dest = Path(dest_root) / retailer
        dest.mkdir(parents=True, exist_ok=True)
        fx_index_path = dest / "index.json"
        fx_index = json.loads(fx_index_path.read_text(encoding="utf-8")) if fx_index_path.is_file() else {}
        fx_index.setdefault("retailer", retailer)
        fixtures = fx_index.setdefault("fixtures", {})
        if name in fixtures and not force:
            raise ProbeError(f"Fixture {retailer}/{name} already exists (use --force to replace it)")
        for old in dest.glob(f"{name}__*"):
            old.unlink()
        files = []
        n = 0
        for rec in index:
            bf = rec.get("body_file")
            if not bf or not (bdir / bf).is_file():
                continue
            n += 1
            fname = f"{name}__{n:02d}{Path(bf).suffix}"
            shutil.copyfile(bdir / bf, dest / fname)
            files.append({"file": fname, **{k: rec.get(k) for k in ("method", "url", "status", "via", "phase", "elapsed_ms")},
                          "content_type": next((v for k, v in (rec.get("headers") or {}).items()
                                                if k.lower() == "content-type"), None)})
        result = doc.get("result") or {}
        fixtures[name] = {
            "source_url": summary.get("url"),
            "captured_at": summary.get("started_at"),
            "retailer_config": summary.get("retailer_config") or doc.get("retailer_config") or {},
            "environment": doc.get("environment"),
            "observed": {k: result.get(k) for k in ("status", "status_text", "price", "title", "error")}
            | {"adapter": summary.get("adapter"), "verdict": summary.get("verdict")},
            "files": files,
        }
        fx_index_path.write_text(json.dumps(fx_index, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return dest


# --------------------------------------------------------------------------- local web UI

INDEX_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Site Probe</title>
<style>
:root{--bg:#f7f7f8;--panel:#fff;--text:#16181d;--muted:#667085;--line:#e4e7ec;--accent:#2563eb;
--ok:#15803d;--okbg:#dcfce7;--fail:#b91c1c;--failbg:#fee2e2;--blk:#a16207;--blkbg:#fef3c7;--q:#6d28d9;--qbg:#ede9fe}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--panel:#171a21;--text:#e6e8ee;--muted:#98a2b3;--line:#2a2f3a;
--accent:#60a5fa;--ok:#4ade80;--okbg:#12301f;--fail:#f87171;--failbg:#3a1616;--blk:#fbbf24;--blkbg:#3a2e0c;--q:#c4b5fd;--qbg:#2a2144}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1180px;margin:0 auto;padding:20px 16px 60px}h1{font-size:20px;margin:0 0 4px}p.sub{color:var(--muted);margin:0 0 18px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:14px}
textarea,input,select{font:inherit;color:inherit;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:7px 9px}
textarea{width:100%;min-height:80px;resize:vertical}label{color:var(--muted);font-size:12px;display:block;margin-bottom:3px}
.row{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end}.row>div{min-width:120px}
button{font:inherit;border:1px solid var(--line);background:var(--panel);color:var(--text);border-radius:6px;padding:7px 12px;cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}button:disabled{opacity:.5;cursor:default}
.stores{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:4px 12px;max-height:220px;overflow:auto;margin-top:8px}
.stores label{display:flex;gap:6px;align-items:center;color:var(--text);font-size:13px;margin:0}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:7px 6px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500;font-size:12px}td.url{max-width:340px;word-break:break-all;font-size:12px}
.b{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;font-weight:600}
.OK{color:var(--ok);background:var(--okbg)}.FAIL{color:var(--fail);background:var(--failbg)}.BLOCKED{color:var(--blk);background:var(--blkbg)}
.QUEUE{color:var(--q);background:var(--qbg)}.PENDING{color:var(--muted);background:var(--line)}
.STALE{color:var(--fail);background:var(--line)}.INFO{color:var(--muted);background:var(--line)}
details{font-size:12px;color:var(--muted)}details pre{white-space:pre-wrap;word-break:break-word;margin:6px 0 0}
.muted{color:var(--muted)}.table-wrap{overflow-x:auto}
</style></head><body><main>
<h1>Stock Watcher site probe</h1>
<p class="sub">Runs the real checkers from this computer. Everything stays in <code>probe-output/</code> until you share the bundle.</p>
<div class="card">
 <label for="urls">Product URLs (one per line)</label>
 <textarea id="urls" placeholder="https://www.target.com/p/.../-/A-12345678"></textarea>
 <div style="margin-top:10px"><div class="muted" style="font-size:12px">Or run the sample URLs (sites.json) for these stores
  <input id="filter" placeholder="filter" aria-label="filter stores" style="margin-left:8px;padding:3px 7px;width:140px">
  <button type="button" id="all" style="padding:3px 9px">all</button> <button type="button" id="none" style="padding:3px 9px">none</button></div>
  <div class="stores" id="stores"></div></div>
</div>
<div class="card row">
 <div><label for="zip">ZIP</label><input id="zip" size="7" inputmode="numeric"></div>
 <div><label for="ful">Fulfillment</label><select id="ful"><option value="">default</option><option>delivery</option><option>pickup</option><option>any</option></select></div>
 <div><label for="radius">Radius (mi)</label><input id="radius" size="4" inputmode="numeric"></div>
 <div><label for="store">Store ID</label><input id="store" size="8"></div>
 <div><label><input type="checkbox" id="preview"> also run preview</label></div>
 <div style="margin-left:auto;display:flex;gap:8px"><button class="primary" id="run">Run checks</button>
 <a id="dl" href="/api/bundle"><button type="button">Download bundle (.zip)</button></a></div>
</div>
<div class="card"><div id="status" class="muted">No checks yet.</div>
<div class="table-wrap"><table><thead><tr><th>Store</th><th>URL</th><th>Status</th><th>Adapter</th><th>Price</th><th>Verdict</th><th>Details</th></tr></thead>
<tbody id="rows"></tbody></table></div></div>
</main>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let meta=null, rows=[];
async function api(path,body){const r=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});
 const j=await r.json().catch(()=>({error:'HTTP '+r.status}));if(!r.ok)throw new Error(j.error||('HTTP '+r.status));return j;}
function renderStores(){const f=$('filter').value.toLowerCase();$('stores').innerHTML=meta.retailers.filter(r=>!f||r.name.toLowerCase().includes(f)||r.key.includes(f))
 .map(r=>`<label title="${esc(r.samples.join('\n'))}"><input type="checkbox" value="${esc(r.key)}" ${r.samples.length?'':'disabled'}>${esc(r.name)} <span class="muted">(${r.samples.length})</span></label>`).join('');}
function render(){$('rows').innerHTML=rows.map(r=>{const s=r.result;const v=s?s.verdict:'PENDING';
 const det=s?`<details><summary>${esc(s.status_text||'')}</summary><pre>${esc([s.verdict_reason&&('verdict: '+s.verdict_reason),s.error&&('error: '+s.error),
 s.seller&&('seller: '+s.seller),s.cart_url&&('cart: '+s.cart_url),(s.available||[]).length&&('available: '+s.available.join('; ')),
 s.fetched_via&&('fetched via '+s.fetched_via+', '+s.requests+' request(s), '+s.duration_s+'s'),...(s.signals||[]).map(x=>'signal: '+x),
 s.bundle&&('saved: probe-output/'+s.bundle)].filter(Boolean).join('\n'))}</pre></details>`:'<span class="muted">running…</span>';
 return `<tr><td>${esc(s?.store||r.key||s?.retailer_name||'')}</td><td class="url">${esc(r.url)}</td><td>${esc(s?.status||'')}</td><td>${esc(s?.adapter||'')}</td>
 <td>${esc(s?.price||'')}</td><td><span class="b ${v}">${v}</span></td><td>${det}</td></tr>`}).join('');}
$('run').onclick=async()=>{const urls=$('urls').value.split(/\s+/).map(s=>s.trim()).filter(Boolean);
 const keys=[...document.querySelectorAll('#stores input:checked')].map(i=>i.value);
 if(!urls.length&&!keys.length){$('status').textContent='Paste a URL or tick a store first.';return}
 const opts={zip:$('zip').value.trim(),fulfillment:$('ful').value,radius_miles:$('radius').value.trim(),store_id:$('store').value.trim(),preview:$('preview').checked};
 $('run').disabled=true;try{const j=await api('/api/run',{urls,keys,options:opts});
 const offset=rows.length;j.entries.forEach(e=>rows.push({url:e.url,key:e.key,result:null}));render();
 const id=j.id;(async()=>{for(;;){const s=await api('/api/job?id='+encodeURIComponent(id));s.results.forEach((x,i)=>{if(x)rows[offset+i].result=x});render();
 const done=s.results.filter(Boolean).length;$('status').textContent=`${done}/${s.results.length} done`+(s.state==='done'?' · report saved to probe-output/report.md':'');
 if(s.state==='done'){$('run').disabled=false;return}await new Promise(r=>setTimeout(r,1000));}})().catch(e=>{$('status').textContent=e.message;$('run').disabled=false});}
 catch(e){$('status').textContent=e.message;$('run').disabled=false}};
$('filter').oninput=renderStores;$('all').onclick=()=>document.querySelectorAll('#stores input:not(:disabled)').forEach(i=>i.checked=true);
$('none').onclick=()=>document.querySelectorAll('#stores input').forEach(i=>i.checked=false);
api('/api/meta').then(m=>{meta=m;renderStores();if(m.note)$('status').textContent=m.note}).catch(e=>$('status').textContent=e.message);
</script></body></html>
"""


class ProbeServer:
    """Runs probes on one long-lived event loop in a background thread (the fetcher's pooled
    client and browser are bound to a loop)."""

    def __init__(self, out_root: Path = DEFAULT_OUT, sites_path: Path = DEFAULT_SITES, concurrency: int = 3):
        self.out_root = Path(out_root)
        self.sites_path = sites_path
        self.concurrency = concurrency
        self.jobs: dict[str, dict] = {}
        self.session_results: list[dict] = []
        self.lock = threading.Lock()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True, name="probe-loop")
        self.thread.start()

    def meta(self) -> dict:
        _, fetcher, registry = backend()
        try:
            sites = load_sites_data(sites_doc_for(self.sites_path, must_exist=True))
        except ProbeError:
            sites = {}
        note = None if callable(getattr(fetcher, "recording", None)) else HOOK_MISSING_MSG
        return {
            "retailers": [{"key": r.key, "name": r.name, "hosts": list(r.hosts), "adapter": r.adapter,
                           "samples": [e["url"] for e in sites.get(r.key, [])]} for r in registry.RETAILERS],
            "environment": environment_info(),
            "note": note,
        }

    def submit(self, body: dict) -> dict:
        urls = [u.strip() for u in body.get("urls") or [] if isinstance(u, str) and u.strip()]
        keys = [k for k in body.get("keys") or [] if isinstance(k, str)]
        bad = [u for u in urls if urlsplit(u).scheme not in ("http", "https")]
        if bad:
            raise ProbeError(f"Not an http(s) URL: {bad[0]}")
        entries = [{"key": None, "url": u, "retailer_config": {}, "note": None} for u in urls]
        if keys:
            entries += select_entries(load_sites_data(sites_doc_for(self.sites_path, must_exist=True)), keys)
        if not entries:
            raise ProbeError("Nothing to run")
        opts = body.get("options") or {}
        base = config_from_options(zip_code=opts.get("zip"), fulfillment=opts.get("fulfillment"),
                                   radius=opts.get("radius_miles"), store_id=opts.get("store_id"))
        job_id = uuid.uuid4().hex[:12]
        job = {"id": job_id, "state": "running", "results": [None] * len(entries), "entries": entries}
        with self.lock:
            self.jobs[job_id] = job

        def on_result(i: int, s: dict) -> None:
            with self.lock:
                job["results"][i] = s

        async def run() -> None:
            try:
                res = await run_entries(entries, concurrency=self.concurrency, preview=bool(opts.get("preview")),
                                        base_config=base, out_root=self.out_root, on_result=on_result)
                with self.lock:
                    self.session_results.extend(res)
                    all_results = list(self.session_results)
                write_report(all_results, self.out_root, sites=self.sites_path)
            except Exception as e:  # noqa: BLE001
                with self.lock:
                    for i, r in enumerate(job["results"]):
                        if r is None:
                            job["results"][i] = error_summary(entries[i], e)
            finally:
                job["state"] = "done"

        asyncio.run_coroutine_threadsafe(run(), self.loop)
        return {"id": job_id, "entries": [{"url": e["url"], "key": e.get("key")} for e in entries]}

    def job(self, job_id: str) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            return {"id": job_id, "state": job["state"], "results": list(job["results"])}

    def close(self) -> None:
        checkers, _, _ = backend()
        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(checkers.shutdown(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)


def make_handler(server: ProbeServer, port_ref: list[int]):
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        server_version = "SiteProbe/1"

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet
            pass

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").lower()
            allowed = {f"127.0.0.1:{port_ref[0]}", f"localhost:{port_ref[0]}", "127.0.0.1", "localhost"}
            return host in allowed  # DNS-rebinding guard

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: Any) -> None:
            self._send(code, json.dumps(_jsonable(obj)).encode("utf-8"), "application/json")

        def do_GET(self) -> None:  # noqa: N802
            if not self._host_ok():
                return self._json(403, {"error": "forbidden host"})
            parts = urlsplit(self.path)
            try:
                if parts.path in ("/", "/index.html"):
                    return self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
                if parts.path == "/api/meta":
                    return self._json(200, server.meta())
                if parts.path == "/api/job":
                    job = server.job(dict(parse_qsl(parts.query)).get("id", ""))
                    return self._json(200, job) if job else self._json(404, {"error": "no such job"})
                if parts.path == "/api/bundle":
                    zpath, _, _ = make_bundle(server.out_root, server.out_root.parent)
                    return self._send(200, zpath.read_bytes(), "application/zip",
                                      {"Content-Disposition": f'attachment; filename="{zpath.name}"'})
                if parts.path == "/api/report.md":
                    p = server.out_root / "report.md"
                    if not p.is_file():
                        return self._json(404, {"error": "no report yet"})
                    return self._send(200, p.read_bytes(), "text/markdown; charset=utf-8")
                return self._json(404, {"error": "not found"})
            except ProbeError as e:
                return self._json(400, {"error": str(e)})

        def do_POST(self) -> None:  # noqa: N802
            if not self._host_ok():
                return self._json(403, {"error": "forbidden host"})
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._json(415, {"error": "JSON only"})  # forces a CORS preflight for cross-site posts
            if urlsplit(self.path).path != "/api/run":
                return self._json(404, {"error": "not found"})
            try:
                n = min(int(self.headers.get("Content-Length") or 0), 1_000_000)
                body = json.loads(self.rfile.read(n) or b"{}")
                return self._json(200, server.submit(body if isinstance(body, dict) else {}))
            except (ProbeError, ValueError) as e:
                return self._json(400, {"error": str(e)})

    return Handler


def start_server(port: int = 8765, out_root: Path = DEFAULT_OUT, sites_path: Path = DEFAULT_SITES,
                 concurrency: int = 3):
    """→ (httpd, ProbeServer). Bound to 127.0.0.1 only. Caller runs httpd.serve_forever()."""
    from http.server import ThreadingHTTPServer

    probe = ProbeServer(out_root, sites_path, concurrency)
    port_ref = [port]
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(probe, port_ref))
    port_ref[0] = httpd.server_address[1]
    return httpd, probe


# --------------------------------------------------------------------------- discover
#
# Finds real, current product URLs for each store so sites.json does not rot: sitemaps first
# (robots.txt -> Sitemap: lines, /sitemap.xml, /sitemap_index.xml, /<locale>/sitemap.xml, indexes
# followed, .xml.gz; through the real browser when plain HTTP is blocked), Shopify's /products.json,
# then pages opened in the real browser: the store's own listing/search pages (DISCOVER_SEEDS), the
# current sample pages (even a stale product page links to live ones) and the homepage, plus the HTML
# sitemap / category pages they link to. Candidates are filtered with a per-store product-URL pattern
# (US locale, no protection plans / gift cards) and verified with the real checker.

DISCOVER_SKIP: dict[str, str] = {}
# Listing / search pages whose product links are the best candidates (opened in the browser before the
# homepage). eBay listings expire, so eBay is re-discovered from a Buy-It-Now search each run.
DISCOVER_SEEDS: dict[str, list[str]] = {
    "ebay": ["https://www.ebay.com/sch/i.html?_nkw=nintendo+switch+2&LH_BIN=1", "https://www.ebay.com/deals"],
    "stockx": ["https://stockx.com/category/electronics", "https://stockx.com/brands/pop-mart"],
    "nvidia": ["https://marketplace.nvidia.com/en-us/consumer/graphics-cards/"],
    "psdirect": ["https://direct.playstation.com/en-us/sitemap", "https://direct.playstation.com/en-us/accessories",
                 "https://direct.playstation.com/en-us/hardware/ps5"],
    # the three listing pages its homepage / not-found page link to (2026-09-29 recordings)
    "nextwarehouse": ["https://www.nextwarehouse.com/sitemap.cfm", "https://www.nextwarehouse.com/categoryList.cfm",
                      "https://www.nextwarehouse.com/mfg_browse.html"],
    "evga": ["https://www.evga.com/products/productlist.aspx.type=10.html"],
    "asus": ["https://shop.asus.com/us/"],
    # AMD retired its amd.com/en/direct-buy/<id>/us pages: on 2026-09-29 they all redirected to the homepage of
    # AMD's BigCommerce store, which links its HTML sitemap and the processor listing
    "amd": ["https://shop-us-en.amd.com/sitemap.php", "https://shop-us-en.amd.com/processors/"],
}
# (all seed pages above were linked from the stores' own pages in the 2026-09-29 recordings; B&H and Home
# Depot get theirs from the sample pages — B&H's stale product page linked 13 live products — and the
# homepage's /b/ and /c/browse listing links)
# Where to look instead of the sample URLs' hosts (NVIDIA: marketplace product pages sell; www.nvidia.com
# GPU pages are spec sheets the checker reports as INFO).
DISCOVER_BASES: dict[str, list[str]] = {
    "nvidia": ["https://marketplace.nvidia.com"],
    "asus": ["https://shop.asus.com"],
    "amd": ["https://shop-us-en.amd.com"],
}
# Stores with no useful public sitemap: straight to the seed pages.
NO_SITEMAP = frozenset({"ebay", "stockx"})
# Only these hosts give candidates (NVIDIA's info pages on www.nvidia.com match the same GPU paths).
CANDIDATE_HOSTS: dict[str, tuple[str, ...]] = {"nvidia": ("marketplace.nvidia.com",)}
MAX_PAGE_REQUESTS = 6  # browser page opens per store (seeds, samples, homepage, listings)
DEFAULT_PER_STORE = 2
DEFAULT_MAX_TRIES = 6
DEFAULT_STORE_TIMEOUT = 90.0
DEFAULT_DISCOVER_CONCURRENCY = 2
DEFAULT_REQUEST_GAP = 0.75  # seconds between our own requests to one host
MAX_SITEMAP_REQUESTS = 14  # per store (robots.txt included)
MAX_SITEMAP_BYTES = 24 * 1024 * 1024  # per store, as downloaded
MAX_SITEMAP_TEXT = 40 * 1024 * 1024  # one file, after gunzip
MAX_LEAF_SITEMAPS = 5
MAX_SITEMAP_DEPTH = 3
POOL_TARGET = 80  # stop reading sitemaps once this many matching URLs are known

# Product-page URL patterns per registry key (regexes, case-insensitive, searched in the URL's
# "/path" and, when that does not match, in "/path?query"). Only URLs on the store's own hosts
# are considered. Keys missing here fall back to GENERIC_PRODUCT_PATTERN.
_SHOPIFY = r"/products/[^/?#]+"
PRODUCT_PATTERNS: dict[str, str] = {
    # AMD's store (shop-us-en.amd.com, BigCommerce): /amd-ryzen-7-9800x3d-processor/-style one-segment slugs with
    # a model number, not its sections; www.amd.com /products/ pages are spec sheets (INFO, never kept)
    "amd": r"^/(?!(?:processors|graphics|adaptive-embedded-computing|gaming-personal-computing|amd-promotions|"
           r"terms-of-purchase|amdonlinestorefaq|sitemap|compare|brands?)(?:[/.]|$))(?=[^/]*\d)[a-z0-9]+(?:-[a-z0-9]+){2,}/?$"
           r"|/products/[^?#]+\.html$",
    # shop.asus.com/us/90mb1ir0-m0aay0-rog-strix-b850-f-gaming-wifi.html (part number + name), also under a
    # section (/us/rog/...): a name with a digit, never CMS pages (/us/id-me-page)
    "asus": r"^/us/(?:[a-z0-9-]+/)?[a-z0-9][a-z0-9-]*\d[a-z0-9-]*\.html$",
    "acegraphics": _SHOPIFY,
    "adorama": r"^/[A-Za-z0-9]{4,}\.html$",
    "amazon": r"/dp/[A-Z0-9]{10}|/gp/product/[A-Z0-9]{10}",
    "antonline": r"^/[^/]+/.+/\d{5,}/?$",
    "bhphoto": r"^/c/product/\d+-REG/[^/?#]+\.html$",  # not .../specs, /print, /ask-question
    "bjs": r"/product/[^/?#]+/\d{8,}",
    "bandai": r"/item/[A-Z]?\d{6,}[A-Z0-9]*",
    "bestbuy": r"/site/.+\.p\?skuId=\d+|/product/.+/sku/\d+",
    "canon": r"/shop/p/[^/?#]+",
    "consutronix": r"/products?/[^/?#]+|/[^/?#]*\d{4,}[^/?#]*\.html$",  # not Shopify (plain 404 page, 2026-09-29)
    "costco": r"\.product\.\d+\.html|/p/-/",
    "dell": r"/(?:spd|apd)/[^/?#]+",
    "disney": r"-\d{8,}\.html$",
    "evga": r"/products/product\.aspx(?:\?|\.)pn=[\w-]+",  # ?pn=220-G7-1000-X1 or the rewritten .pn=...html
    "fujifilm": r"^/[a-z0-9]+(?:-[a-z0-9]+){1,}/?$",
    "gamefly": r"/(?:game|product|gear)/[^/?#]+/\d+",
    "gamestop": r"/products/.+/\d+\.html",
    "gigabyte": r"/(?:Graphics-Card|Motherboard|Laptop|Monitor|Gaming-PCs?|Mini-PcBarebone)/(?=[^/?#]*\d)[A-Za-z0-9][\w.-]*/?$",
    "govee": _SHOPIFY,
    "hallmark": r"-[0-9A-Z]{6,}\.html$",
    "homedepot": r"^/p/(?:[^/?#]+/)?\d{9}/?$",
    "jazwares": _SHOPIFY,
    "kohls": r"/product/prd-\d+/",
    "kroger": r"/p/[^/?#]+/\d{10,}",
    "lg": r"^/us/[a-z0-9-]+/[a-z0-9]+-[a-z0-9-]*\d[a-z0-9-]*/?$",
    "lego": r"/product/[^/?#]+-\d{4,}",
    "leica": r"/en-US/(?:photography|sport-optics|observation)/[^?#]+/[^/?#]+$",
    "lenovo": r"/p/[^?#]+/[0-9a-z]{8,}",
    "msi": r"^/[^/]+/[^/]+/.*[^/?#]*\d[^/?#]*/?$",
    "mattel": _SHOPIFY,
    "meijer": r"/shopping/product/[^/?#]+/\d+",
    "microcenter": r"/product/\d+/",
    "microsoft": r"/d/[^/?#]+/[A-Za-z0-9]{12}",
    "xbox": r"/configure/[A-Z0-9]{12}|/games/store/[^/?#]+/[A-Z0-9]{12}",
    "nyxi": _SHOPIFY,
    "neutronusa": _SHOPIFY,
    "newegg": r"/p/(?:N82E\d+|[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4,5}|[0-9A-Z]{15})",
    # /item/?2573476_g10e (a long id, usually with a _suffix; the query is the whole address); also ?p_num=123-style ids.
    # The pattern is loose on purpose: no real item page was ever recorded (the item URLs in the 2026-09-29 runs were
    # invented and answered the site's "page ... no longer in our system" page), so any /item/? address with an id counts.
    "nextwarehouse": r"/item/\?[^#]*\d{5,}",
    "ninja": r"/pdp/[^/?#]+/[^/?#]+\.html",
    "nintendo": r"/us/store/products/[^/?#]+",
    # marketplace product pages only (CANDIDATE_HOSTS): /en-us/consumer/graphics-cards/nvidia-geforce-rtx-5090/
    "nvidia": r"/graphics-cards/(?:[a-z0-9-]+/)*[a-z0-9-]*(?:rtx|gtx)-?\d{3,4}[a-z0-9-]*/?$",
    "oculus": r"/quest/[a-z0-9-]+(?:/[a-z0-9-]+)?/?$",
    "officedepot": r"/a/products/\d+/",
    "popmart": r"/products/\d+/",
    "playasia": r"^/[^/?#]+/\d+/[0-9a-z]{6,}/?$",
    # consoles, accessories and games: /en-us/<section>/<name>.<product code>, and the code-less
    # /en-us/buy-consoles/playstation5-pro-console-2-tb pages the 2026-09-29 menus link to
    "psdirect": r"^/en-us/(?:[a-z0-9-]+/)+[a-z0-9-]+\.\d{7,}/?$|^/en-us/buy-[a-z-]+/[a-z0-9-]+/?$",
    "pokemoncenter": r"/product/[\d-]+/[^/?#]+",
    "qvc": r"\.product\.[A-Z]?\d+\.html",
    "robertscamera": r"^/[a-z0-9]+(?:-[a-z0-9]+){3,}/?$",
    "samsclub": r"/ip/(?:.+/)?\d{6,}",
    # one-segment slugs (stockx.com/nintendo-switch-2-console-us-version), not the site's own sections
    "stockx": r"^/(?!(?:brands?|category|browse|help|search|login|signup|sell|news|about|live|listings|dp|buy|"
              r"stockx-gift-card|sneakers|apparel|electronics|collectibles|trading-cards|accessories|"
              r"retail-price|release-dates?|careers|privacy|terms)(?:[/-]|$))[a-z0-9]+(?:-[a-z0-9]+){2,}/?$",
    "target": r"/p/.+/-/A-\d+",
    "toysrus": _SHOPIFY,
    "verizon": r"/(?:smartphones|tablets|smartwatches|connected-devices|accessories|home-internet)/[^/?#]+/?$",
    "walmart": r"/ip/(?:.+/)?\d{6,}",
    "ebay": r"^/itm/(?:[^/?#]+/)?\d{9,}/?$",
    "zotac": r"/us/[a-z0-9]+(?:-[a-z0-9]+){2,}/?$",
}
GENERIC_PRODUCT_PATTERN = (
    r"/products?/[^/?#]+|/p/[^/?#]+|/dp/[^/?#]+|/ip/[^/?#]+|/item/[^/?#]+|/[^/?#]*\d{4,}[^/?#]*\.html$"
)
SHOPIFY_KEYS = frozenset(k for k, v in PRODUCT_PATTERNS.items() if v == _SHOPIFY)

_DENY_PATH = re.compile(
    r"\.(?:jpe?g|png|gif|webp|svg|css|js|json|pdf|xml|gz|zip|ico|mp4|woff2?)$"
    r"|/(?:cart|checkout|account|login|signin|sign-in|register|wishlist|search|reviews?|gift-cards?)(?:/|$)"
    # not stock-watching material: protection / service plans (Micro Center's 2-year-accidental-damage-
    # protection-plan was picked on 2026-09-29), warranties, gift cards
    r"|(?:protection|service|replacement|care|damage)[-_]?plans?\b|\bwarrant(?:y|ies)\b|gift[-_]?cards?\b|\be[-_]?gift",
    re.I,
)
# a locale segment other than the US one (NVIDIA's /en-sg/ GPU pages were chosen on 2026-09-29; Newegg's
# /global/uk-en/p/... pages on 2026-09-30, its US pages have no prefix): /en-gb/, /uk-en/, /ca-en/, /global/<locale>/
_FOREIGN_LOCALE = re.compile(r"^/(?:global(?:/|$)|(?!(?:en[-_]us|us[-_]en)(?:/|$))[a-z]{2}[-_][a-z]{2}(?:/|$))", re.I)
_compiled: dict[str, re.Pattern[str]] = {}


def product_pattern(key: str) -> re.Pattern[str]:
    """The compiled product-URL regex for a registry key (generic heuristics for unknown keys)."""
    pat = _compiled.get(key)
    if pat is None:
        pat = _compiled[key] = re.compile(PRODUCT_PATTERNS.get(key) or GENERIC_PRODUCT_PATTERN, re.I)
    return pat


def normalize_candidate(url: str, key: str) -> str | None:
    """A clean https product URL when ``url`` is a product page of store ``key``, else None.
    The fragment is dropped; the query only when the pattern needs it (skuId=, pn=, p_num=)."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    _, _, registry = backend()
    r = registry.match_retailer(url)
    if r is None or r.key != key:
        return None
    path = parts.path or "/"
    if _DENY_PATH.search(path) or _FOREIGN_LOCALE.search(path):
        return None
    hosts = CANDIDATE_HOSTS.get(key)
    if hosts and parts.hostname.lower() not in hosts:
        return None
    pat = product_pattern(key)
    if pat.search(path):
        query = ""
    elif parts.query and pat.search(f"{path}?{parts.query}"):
        query = parts.query
    else:
        return None
    return urlunsplit(("https", parts.netloc, path, query, ""))


# ---- robots.txt / sitemaps

_SITEMAP_LINE = re.compile(r"^\s*sitemap\s*:\s*(\S+)", re.I | re.M)
_LOC = re.compile(r"<(?:\w+:)?loc\b[^>]*>(.*?)</(?:\w+:)?loc>", re.I | re.S)
_LASTMOD = re.compile(r"<(?:\w+:)?lastmod\b[^>]*>(.*?)</(?:\w+:)?lastmod>", re.I | re.S)
_ENTRY = re.compile(r"<(?:\w+:)?(url|sitemap)\b[^>]*>(.*?)</(?:\w+:)?\1\s*>", re.I | re.S)
_INDEX_TAG = re.compile(r"<\s*(?:\w+:)?sitemapindex\b", re.I)


def parse_robots_sitemaps(text: str, base: str = "") -> list[str]:
    """The ``Sitemap:`` URLs listed in a robots.txt (relative ones resolved against ``base``)."""
    out: list[str] = []
    for m in _SITEMAP_LINE.finditer(text or ""):
        u = urljoin(base, m.group(1).strip()) if base else m.group(1).strip()
        if u.startswith(("http://", "https://")) and u not in out:
            out.append(u)
    return out


def gunzip_limited(data: bytes, limit: int = MAX_SITEMAP_TEXT) -> bytes:
    """Decompress gzip data (up to ``limit`` bytes out); b"" when it is not valid gzip."""
    try:
        return zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(data, limit)
    except zlib.error:
        return b""


@dataclasses.dataclass
class Sitemap:
    kind: str  # "index" (lists other sitemaps) or "urlset"
    entries: list[tuple[str, str | None]]  # (loc, lastmod)


def parse_sitemap(data: bytes | str) -> Sitemap:
    """Parse a sitemap or sitemap index (plain, gzipped, or a plain-text URL list)."""
    import html as _html

    if isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
        if raw[:2] == b"\x1f\x8b":
            raw = gunzip_limited(raw)
        text = raw.decode("utf-8", "replace")
    else:
        text = data
    kind = "index" if _INDEX_TAG.search(text) else "urlset"

    def clean(s: str) -> str:
        s = s.strip()
        if s.startswith("<![CDATA["):
            s = s[9:].rsplit("]]>", 1)[0]
        return _html.unescape(s.strip())

    entries: list[tuple[str, str | None]] = []
    for m in _ENTRY.finditer(text):
        loc = _LOC.search(m.group(2))
        if not loc:
            continue
        lm = _LASTMOD.search(m.group(2))
        entries.append((clean(loc.group(1)), clean(lm.group(1)) if lm else None))
    if not entries:
        entries = [(clean(m.group(1)), None) for m in _LOC.finditer(text)]
    if not entries and "<" not in text[:200]:  # plain-text sitemap
        entries = [(ln.strip(), None) for ln in text.splitlines() if ln.strip().startswith(("http://", "https://"))]
    return Sitemap(kind, [e for e in entries if e[0].startswith(("http://", "https://"))])


_SM_GOOD = re.compile(r"product|/pdp|pdps|item|sku|goods|listing|/p[-_.]", re.I)
_SM_BAD = re.compile(r"blog|article|news|page|categor|collection|image|video|brand|store|location|cms|content|"
                     r"help|support|review|faq|tag|manufactur|landing|media|forum", re.I)
_SM_US = re.compile(r"en[-_]us|[-_/]us[-_./]", re.I)
_SM_OTHER_LOCALE = re.compile(r"[-_/](?:fr|de|es|it|ja|ko|zh|pt|nl|sv|da|nb|fi|pl|tr)[-_./]|[-_](?:ca|uk|gb|au|in|mx|br|jp)\b", re.I)


def sitemap_rank(url: str) -> int:
    """Higher = read first: product-looking names and US locales win; blog/category/image sitemaps lose."""
    name = urlsplit(url).path
    score = 0
    if _SM_GOOD.search(name):
        score += 3
    if _SM_BAD.search(name):
        score -= 2
    if _SM_US.search(name):
        score += 1
    if _SM_OTHER_LOCALE.search(name):
        score -= 1
    return score


@dataclasses.dataclass
class Budget:
    """Caps the requests and bytes one store's discovery may spend."""
    max_requests: int = MAX_SITEMAP_REQUESTS
    max_bytes: int = MAX_SITEMAP_BYTES
    requests: int = 0
    bytes: int = 0

    @property
    def left(self) -> bool:
        return self.requests < self.max_requests and self.bytes < self.max_bytes


async def budgeted_get(net: Any, budget: Budget, url: str) -> tuple[int | None, bytes]:
    """``net.get`` that counts against the budget and never raises; (None, b"") when over budget."""
    if not budget.left:
        return None, b""
    budget.requests += 1
    try:
        status, body = await net.get(url)
    except Exception:  # noqa: BLE001 - one bad request must not end discovery
        return None, b""
    body = body or b""
    budget.bytes += len(body)
    return status, body


_LOCALE_PREFIX = re.compile(r"^/((?:[a-z]{2}[-_][a-z]{2}|us)(?:/[a-z]{2})?)/", re.I)


def locale_sitemaps(base: str, samples: list[dict] | None) -> list[str]:
    """``{base}/<locale>/sitemap.xml`` for the locale prefixes of the store's sample URLs on ``base``
    (Magento / AEM stores such as shop.asus.com/us/ keep their sitemap under the store view)."""
    out: list[str] = []
    for smp in samples or []:
        u = urlsplit(smp.get("url") or "")
        if f"{u.scheme}://{u.netloc}" != base:
            continue
        m = _LOCALE_PREFIX.match(u.path or "")
        if m and f"{base}/{m.group(1)}/sitemap.xml" not in out:
            out.append(f"{base}/{m.group(1)}/sitemap.xml")
    return out


async def collect_from_sitemaps(net: Any, budget: Budget, base: str, key: str, pool: dict[str, str | None],
                                notes: list[str], *, want: int = POOL_TARGET, max_leaves: int = MAX_LEAF_SITEMAPS,
                                extra_seeds: list[str] | None = None) -> int:
    """Read ``base``'s sitemaps and add matching product URLs to ``pool`` ({url: lastmod}).
    Returns how many were added."""
    start = len(pool)
    status, body = await budgeted_get(net, budget, f"{base}/robots.txt")
    seeds = parse_robots_sitemaps(body.decode("utf-8", "replace"), base) if status == 200 else []
    for extra in (f"{base}/sitemap.xml", f"{base}/sitemap_index.xml", *(extra_seeds or [])):
        if extra not in seeds:
            seeds.append(extra)
    queue: list[tuple[str, int]] = [(u, 0) for u in seeds]
    seen: set[str] = set()
    leaves = 0
    missing = 0
    while queue and budget.left and leaves < max_leaves and len(pool) - start < want:
        queue.sort(key=lambda it: (-sitemap_rank(it[0]), it[1]))  # stable: robots order kept among equals
        url, depth = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        status, body = await budgeted_get(net, budget, url)
        if status != 200 or not body:
            missing += 1
            continue
        sm = parse_sitemap(body)
        if sm.kind == "index":
            if depth + 1 <= MAX_SITEMAP_DEPTH:
                queue.extend((loc, depth + 1) for loc, _ in sm.entries if loc not in seen)
            continue
        leaves += 1
        for loc, lastmod in sm.entries:
            cand = normalize_candidate(loc, key)
            if cand and cand not in pool:
                pool[cand] = lastmod
                if len(pool) - start >= want:
                    break
    if len(pool) == start:
        notes.append(f"sitemaps: no {key} product URLs ({missing} of {len(seen)} sitemap request(s) failed)"
                     if seen else "sitemaps: none reachable")
    return len(pool) - start


async def collect_from_shopify(net: Any, budget: Budget, base: str, key: str, pool: dict[str, str | None]) -> int:
    """Shopify storefronts publish /products.json (real handles + updated_at)."""
    status, body = await budgeted_get(net, budget, f"{base}/products.json?limit=60")
    if status != 200 or not body:
        return 0
    try:
        products = json.loads(body.decode("utf-8", "replace")).get("products") or []
    except (ValueError, AttributeError):
        return 0
    added = 0
    for p in products:
        handle = p.get("handle") if isinstance(p, dict) else None
        cand = normalize_candidate(f"{base}/products/{handle}", key) if handle else None
        if cand and cand not in pool:
            pool[cand] = p.get("updated_at")
            added += 1
    return added


# ---- homepage links

class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v:
                    self.hrefs.append(v)


_EMBEDDED_PATH = re.compile(r"""["'](/[^"'\s<>\\]{6,240})["']""")


def extract_links(html: str, base: str) -> list[str]:
    """Absolute URLs of every <a href> in ``html`` plus root-relative paths embedded in scripts/JSON."""
    parser = _LinkParser()
    with contextlib.suppress(Exception):
        parser.feed(html or "")
    hrefs = list(parser.hrefs)
    hrefs += _EMBEDDED_PATH.findall((html or "").replace("\\/", "/"))
    out: list[str] = []
    seen: set[str] = set()
    for h in hrefs:
        h = h.strip()
        if not h or h.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        u = urljoin(base + "/", h)
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


_LISTING_HINT = re.compile(
    r"/(?:collections?|c|b|category|categories|browse|shop|deals|new|new-arrivals|best-?sellers?|gaming|toys|"
    r"video-games|electronics|cameras|graphics-cards|consoles|laptops|tvs|products|accessories|hardware)(?:/|$)"
    # old-style .cfm/.aspx listing pages: /cat.cfm?c=517, /category.cfm, /mfg.cfm (NextWarehouse)
    r"|/(?:cat|cats|category|categories|mfg|manufacturers?|dept|departments?)(?:\.\w+)?(?:/|$)"
    # HTML sitemaps and old-style listing pages (NextWarehouse /sitemap.cfm, /categoryList.cfm, /mfg_browse.html;
    # EVGA /products/productlist.aspx.type=10.html; PS Direct /en-us/sitemap)
    r"|/[^/]*(?:sitemap|categorylist|productlist|mfg_browse)[^/]*$", re.I)
_HTML_SITEMAP = re.compile(r"sitemap", re.I)
# stores whose HTML sitemap / category list is two levels above the products (sitemap → category → subcategory → items)
DEEP_LISTING_STORES = frozenset({"nextwarehouse"})


_UTILITY_PATH = re.compile(
    r"about|contact|help|faq|support|polic|privacy|terms|legal|return|shipping|career|login|account|cart|checkout"
    r"|order|track|store-?locator|affiliate|press|news|blog|warrant|rebate|financ|credit|subscribe|newsletter", re.I)


def pick_listing_links(links: list[str], key: str, limit: int = 2, *, relaxed: bool = False) -> list[str]:
    """Same-store category/listing pages worth opening when a page itself shows no products. ``relaxed`` (links
    taken from an HTML sitemap or a listing page): query-string pages (NextWarehouse's .cfm?...) and pages
    without a listing word count too, after those with one — never account / help / policy pages."""
    _, _, registry = backend()
    hinted: list[str] = []
    other: list[str] = []
    for u in links:
        try:
            parts = urlsplit(u)
        except ValueError:
            continue
        r = registry.match_retailer(u)
        if r is None or r.key != key or _DENY_PATH.search(parts.path) or _FOREIGN_LOCALE.search(parts.path):
            continue
        if product_pattern(key).search(parts.path) or (parts.query and product_pattern(key).search(
                f"{parts.path}?{parts.query}")):
            continue
        if parts.query and not relaxed:
            continue
        u2 = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query if relaxed else "", ""))
        if _LISTING_HINT.search(parts.path):
            if u2 not in hinted:
                hinted.append(u2)
        elif relaxed and parts.path.strip("/") and not _UTILITY_PATH.search(f"{parts.path}?{parts.query}") \
                and not generic_home(parts.path) and u2 not in other:
            other.append(u2)
    # an HTML sitemap lists every product: open it first
    hinted.sort(key=lambda u: 0 if _HTML_SITEMAP.search(urlsplit(u).path) else 1)
    return (hinted + other)[:limit]


def generic_home(path: str) -> bool:
    return bool(re.fullmatch(r"/?(?:[a-z]{2}[-_][a-z]{2}/?)?(?:index\.\w+|default\.\w+|home\.\w+)?", path or "/", re.I))


def _origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


async def collect_from_pages(net: Any, budget: Budget, base: str, key: str, pool: dict[str, str | None],
                             notes: list[str], seeds: list[str] | None = None, *, want: int = 12) -> int:
    """Open pages in the real browser and collect product links: ``seeds`` first (the store's own listing /
    search pages, then its current sample pages — a stale product page still links to live ones), then the
    homepage. A page that shows no product links has up to two listing pages it links to opened in turn (an
    HTML sitemap first; one level down from a seed or the homepage — NextWarehouse's sitemap.cfm lists
    categories, not products). Stops once ``want`` product links are known or the budget is spent."""
    start = len(pool)

    def harvest(links: list[str]) -> int:
        n = 0
        for u in links:
            cand = normalize_candidate(u, key)
            if cand and cand not in pool:
                pool[cand] = None
                n += 1
        return n

    async def page(url: str) -> str:
        budget.requests += 1
        try:
            html = await net.page(url)
        except Exception as e:  # noqa: BLE001
            notes.append(f"page {url}: {str(e).splitlines()[0][:120] if str(e) else type(e).__name__}")
            return ""
        budget.bytes += len(html or "")
        return html or ""

    home = f"{base}/"
    queue: list[tuple[str, int, bool]] = [(u, 0, True) for u in (seeds or [])]  # (url, depth, is_seed)
    opened: set[str] = set()
    home_queued = False
    while budget.left and len(pool) - start < want:
        if not queue:
            if home_queued:
                break
            queue.append((home, 0, False))
            home_queued = True
        url, depth, is_seed = queue.pop(0)
        if url in opened:
            continue
        opened.add(url)
        html = await page(url)
        origin = _origin(url)
        links = extract_links(html, origin)
        if harvest(links) or depth >= (2 if key in DEEP_LISTING_STORES else 1) or not links:
            continue
        # no products here: its listing pages next (before the homepage), relaxed for listing-type seeds
        relaxed = is_seed and not product_pattern(key).search(urlsplit(url).path) or bool(
            _LISTING_HINT.search(urlsplit(url).path))
        children = [c for c in pick_listing_links(links, key, relaxed=relaxed) if c not in opened]
        queue[0:0] = [(c, depth + 1, False) for c in children]
    if len(pool) == start:
        notes.append("pages: no product links found" if seeds else "homepage: no product links found")
    return len(pool) - start


# ---- candidates, verification, results

def select_candidates(pool: list[tuple[str, str | None]], limit: int, seed: str = "") -> list[str]:
    """Up to ``limit`` URLs to try: sitemap URLs, the most recently modified first (a fresh lastmod means the
    page is live), shuffled among themselves (deterministically) so in- and out-of-stock items both turn up;
    and — for at least a third of the tries — URLs linked from the store's pages (no lastmod), which its menus
    show today (a big sitemap of old items must not crowd them out)."""
    import random

    rng = random.Random(seed)
    seen: set[str] = set()
    dated: list[tuple[str, str]] = []
    undated: list[str] = []
    for url, lastmod in pool:
        if url in seen:
            continue
        seen.add(url)
        (dated.append((url, lastmod)) if lastmod else undated.append(url))  # type: ignore[arg-type]
    dated.sort(key=lambda p: p[1], reverse=True)
    top = [u for u, _ in dated[: max(limit * 4, limit)]]
    rng.shuffle(top)
    rng.shuffle(undated)
    share = min(len(undated), max(1, limit // 3) if limit >= 2 else 0)
    head = top[: limit - share] if len(top) > limit - share else top
    out = head + undated[: limit - len(head)]
    return (out + [u for u in top if u not in out])[:limit]


_DEAD_END_RE = re.compile(r"discontinued|no longer (?:available|sold|carried)|removed from sale|not available", re.I)


def _dead_end(s: dict) -> bool:
    return s.get("status") == "out_of_stock" and bool(_DEAD_END_RE.search(str(s.get("verdict_reason") or "")
                                                                           + " " + str(s.get("status_text") or "")))


def _definite(s: dict) -> bool:
    return s.get("verdict") == "OK" and s.get("status") in ("in_stock", "out_of_stock")


def choose_verified(verified: list[tuple[str, dict]], per_store: int) -> list[dict]:
    """Pick the URLs to keep from checked candidates ``[(url, summary)]``.

    Definite answers (in_stock / out_of_stock) win, one of each first when asking for two or more; if
    nothing was definite, BLOCKED / QUEUE ones (real pages the checker could not read, flagged
    ``definite: False``) stand in; FAIL ones (404, wrong page, errors) are never kept."""
    def row(url: str, s: dict, definite: bool) -> dict:
        return {"url": url, "status": s.get("status"), "verdict": s.get("verdict"), "price": s.get("price"),
                "definite": definite, "reason": s.get("verdict_reason")}

    # a discontinued / no-longer-available item is a definite answer but a poor sample: it never comes back
    good = sorted(((u, s) for u, s in verified if _definite(s)), key=lambda p: _dead_end(p[1]))
    chosen: list[tuple[str, dict]] = []
    ins = [p for p in good if p[1].get("status") == "in_stock"]
    outs = [p for p in good if p[1].get("status") == "out_of_stock"]
    for group in ((ins, outs) if per_store >= 2 else (ins + outs,)):
        if group and len(chosen) < per_store:
            chosen.append(group[0])
    for p in good:
        if len(chosen) < per_store and p not in chosen:
            chosen.append(p)
    if chosen:
        return [row(u, s, True) for u, s in chosen]
    soft = [(u, s) for u, s in verified if s.get("verdict") in ("BLOCKED", "QUEUE")]
    return [row(u, s, False) for u, s in soft[:per_store]]


def _enough(verified: list[tuple[str, dict]], per_store: int) -> bool:
    good = [s for _, s in verified if _definite(s) and not _dead_end(s)]
    both = {"in_stock", "out_of_stock"} <= {s.get("status") for s in good}
    return len(good) >= per_store and (per_store < 2 or both or len(verified) >= 4)


@dataclasses.dataclass
class StoreResult:
    key: str
    name: str = ""
    source: str = ""  # sitemap, shopify, homepage (joined with "+") or ""
    found: int = 0  # matching product URLs discovered
    tried: int = 0  # candidates checked
    chosen: list[dict] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)
    skipped: str | None = None


def store_bases(retailer: Any, samples: list[dict] | None) -> list[str]:
    """Origins to look for sitemaps on: DISCOVER_BASES, else the hosts of the store's current sample URLs, else
    its registry host."""
    if retailer.key in DISCOVER_BASES:
        return list(DISCOVER_BASES[retailer.key])
    bases: list[str] = []
    for s in samples or []:
        host = urlsplit(s.get("url") or "").hostname
        if host and CANDIDATE_HOSTS.get(retailer.key) and host not in CANDIDATE_HOSTS[retailer.key]:
            continue
        if host and f"https://{host}" not in bases:
            bases.append(f"https://{host}")
    if not bases:
        host = retailer.hosts[0]
        bases.append(f"https://{'www.' + host if host.count('.') == 1 else host}")
    return bases[:2]


def registry_key(url: str) -> str | None:
    _, _, registry = backend()
    r = registry.match_retailer(url)
    return r.key if r is not None else None


async def verify_url(url: str, retailer_config: dict | None = None) -> dict:
    """The real check (checkers.run_check through probe_one), no bundle saved."""
    return await probe_one(url, retailer_config or None, None, preview=False, out_root=None)


async def discover_store(retailer: Any, samples: list[dict] | None, net: Any, *, per_store: int = DEFAULT_PER_STORE,
                         max_tries: int = DEFAULT_MAX_TRIES, time_cap: float = DEFAULT_STORE_TIMEOUT,
                         check: Callable[[str, dict | None], Any] | None = None, base_config: dict | None = None,
                         batch: int = 2, min_check_time: float = 10.0,
                         clock: Callable[[], float] = time.monotonic) -> StoreResult:
    """Find up to ``per_store`` real, current product URLs for one store (see the section comment)."""
    key = retailer.key
    res = StoreResult(key=key, name=retailer.name)
    if key in DISCOVER_SKIP or getattr(retailer, "retired", None):
        res.skipped = DISCOVER_SKIP.get(key) or "no longer sells online (registry: retired)"
        return res
    check = check or verify_url
    t_end = clock() + time_cap
    budget = Budget()
    pool: dict[str, str | None] = {}
    sources: list[str] = []

    async def sitemaps_phase(bases: list[str]) -> None:
        for base in bases:
            if await collect_from_sitemaps(net, budget, base, key, pool, res.notes,
                                           extra_seeds=locale_sitemaps(base, samples)) \
                    and "sitemap" not in sources:
                sources.append("sitemap")

    async def gather_pool() -> None:
        bases = store_bases(retailer, samples)
        if key not in NO_SITEMAP:
            # sitemaps get their own share of the time: through the browser (bot-walled stores) each file
            # costs a page load, and on 2026-09-29 slow sitemaps used up the whole pool phase for B&H and
            # PS Direct, so the pages their menus / stale samples link to were never opened
            try:
                await asyncio.wait_for(sitemaps_phase(bases), timeout=max(1.0, time_cap * 0.3))
            except asyncio.TimeoutError:
                res.notes.append("sitemaps: time share used up")
        if len(pool) < max_tries and key in SHOPIFY_KEYS:
            if await collect_from_shopify(net, budget, bases[0], key, pool):
                sources.append("shopify")
        # the store's listing / search pages, then its current sample pages (stale ones included: they still
        # link to live products), then the homepage — always, on their own budget: links a store's menus show
        # today are fresher than a sitemap's (smaller budget when the sitemaps already gave plenty)
        seeds = list(DISCOVER_SEEDS.get(key, []))
        for smp in samples or []:
            u = smp.get("url")
            if u and u not in seeds and registry_key(u) == key:
                seeds.append(u)
        pages = MAX_PAGE_REQUESTS if len(pool) < max_tries else 2
        if await collect_from_pages(net, Budget(max_requests=pages), bases[0], key, pool, res.notes, seeds):
            sources.append("pages" if seeds else "homepage")

    try:
        await asyncio.wait_for(gather_pool(), timeout=max(1.0, time_cap * 0.6))
    except asyncio.TimeoutError:
        res.notes.append("discovery time cap reached")
    res.source = "+".join(sources)
    res.found = len(pool)
    cands = select_candidates(list(pool.items()), max_tries, key)
    verified: list[tuple[str, dict]] = []

    async def verify(url: str) -> tuple[str, dict]:
        try:
            s = await asyncio.wait_for(check(url, dict(base_config or {})), timeout=max(min_check_time, t_end - clock()))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            s = {"status": "error", "verdict": "FAIL", "verdict_reason": f"{type(e).__name__}: {e}" if str(e) else "timed out"}
        return url, s

    for i in range(0, len(cands), max(1, batch)):
        if i and t_end - clock() < 5:
            res.notes.append("store time cap reached")
            break
        verified += await asyncio.gather(*(verify(u) for u in cands[i:i + max(1, batch)]))
        if _enough(verified, per_store):
            break
    res.tried = len(verified)
    res.chosen = choose_verified(verified, per_store)
    if not res.chosen:
        res.notes.append("no candidate gave a usable answer" if verified else "no candidates to check")
    return res


# ---- the network, as discovery sees it

class LiveNet:
    """Fetches through the backend's fetcher: plain HTTP first (impersonating Chrome for hosts that need it),
    the real browser (same persistent profile the checkers use, so bot walls stay passed) when that is blocked."""

    def __init__(self, gap: float = DEFAULT_REQUEST_GAP):
        self.gap = gap
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._browser_hosts: set[str] = set()

    async def _pause(self, url: str) -> None:
        host = urlsplit(url).hostname or ""
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = self._last.get(host, 0.0) + self.gap - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[host] = time.monotonic()

    async def get(self, url: str) -> tuple[int | None, bytes]:
        _, fetcher, _ = backend()
        host = urlsplit(url).hostname or ""
        await self._pause(url)
        resp = None
        if host not in self._browser_hosts:
            try:
                resp = await fetcher.http_get(url, headers={"Accept": "application/xml,text/xml,text/plain,*/*"})
            except fetcher.FetchError:
                resp = None
            if resp is not None:
                body = resp.content
                head = body[:60_000].decode("utf-8", "replace")
                blocked = resp.status_code in (403, 429, 503) or fetcher.looks_like_challenge(head)
                if resp.status_code in (404, 410):
                    return resp.status_code, b""
                is_html = head.lstrip()[:15].lower().startswith(("<!doctype html", "<html"))
                if is_html and self._walled(url):
                    # a bot-walled store (Home Depot's Akamai "Oops!! Something went wrong", B&H...) answering
                    # a sitemap with an HTML page: that's the wall, not a missing file — ask the browser
                    blocked = True
                if not blocked:
                    if is_html:
                        return 404, b""  # an HTML page where XML/text was expected: soft 404
                    return resp.status_code, (body if resp.status_code < 400 else b"")
        if fetcher.browser_enabled():
            self._browser_hosts.add(host)
            binary = urlsplit(url).path.lower().endswith(".gz")  # read as text, gzip bytes come back mangled
            try:
                status, text, _ = await fetcher.browser_fetch_from_page(
                    f"https://{host}/", url, accept="application/xml,text/xml,text/plain,*/*",
                    **({"binary": True} if binary else {}))
                if binary:
                    return status, base64.b64decode(text or "")
            except Exception:  # noqa: BLE001
                return None, b""
            return status, text.encode("utf-8", "replace")
        return (resp.status_code if resp is not None else None), b""

    @staticmethod
    def _walled(url: str) -> bool:
        """Stores the registry marks as needing the real browser."""
        _, _, registry = backend()
        r = registry.match_retailer(url)
        return bool(r is not None and getattr(r, "browser", False))

    async def page(self, url: str) -> str:
        _, fetcher, _ = backend()
        await self._pause(url)
        res = await fetcher.fetch_html(url, render_js=fetcher.browser_enabled(), needs=None)
        return res.text


# ---- table, merge and write

def format_discovery(results: list[StoreResult]) -> str:
    """A table: one row per kept URL (or one row saying why a store has none)."""
    rows = [("store", "source", "found", "tried", "status", "url")]
    for r in results:
        if r.skipped:
            rows.append((r.key, "skipped", "", "", "", f"({r.skipped})"))
        elif not r.chosen:
            rows.append((r.key, r.source or "-", str(r.found), str(r.tried), "none", "; ".join(r.notes) or "nothing found"))
        else:
            for i, c in enumerate(r.chosen):
                status = c["status"] if c["definite"] else f"{c['verdict']}?"
                rows.append((r.key if i == 0 else "", r.source if i == 0 else "", str(r.found) if i == 0 else "",
                             str(r.tried) if i == 0 else "", str(status), c["url"]))
    widths = [max(len(row[i]) for row in rows) for i in range(5)]
    lines = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(row[:5])) + "  " + row[5] for row in rows]
    lines.insert(1, "-" * min(100, len(lines[0])))
    return "\n".join(lines)


def _entry_cfg(e: Any) -> dict:
    return dict(e.get("retailer_config") or {}) if isinstance(e, dict) else {}


def _sites_of(doc: Any) -> dict:
    if not isinstance(doc, dict):
        return {}
    return doc.get("sites") if isinstance(doc.get("sites"), dict) else {k: v for k, v in doc.items() if not k.startswith("_")}


# Sample URLs sites.json used to ship that are dead now (2026-09-29 sweeps): a sites.local.json written before
# `_discovered` existed that still carries one for a store only copied the old samples there.
REPLACED_SAMPLES = frozenset({
    "https://www.amd.com/en/direct-buy/5335621300/us",
    "https://www.bhphotovideo.com/c/product/1809440-REG/fujifilm_16821474_x100vi_digital_camera_silver.html",
    "https://direct.playstation.com/en-us/buy-consoles/playstation5-pro-console.3009726",
})


def _entry_urls(entries: Any) -> set[str]:
    entries = entries if isinstance(entries, list) else [entries]
    return {e if isinstance(e, str) else str((e or {}).get("url") or "") for e in entries}


def overlay_shipped(local: dict, shipped: dict) -> dict:
    """The sites document a sweep / discover uses when sites.local.json exists: its stores, except those it only
    holds a copy of the shipped samples for (no discovery found them: not in ``_discovered``; for an older local
    file, one still carrying a REPLACED_SAMPLES link) — those, and stores new in sites.json, take the current
    shipped entries, so fixed samples reach users who already have a local file. The shipped ``_README`` /
    ``_comment`` are carried."""
    sites = dict(_sites_of(local))
    legacy = "_discovered" not in local
    found = {k for k in local.get("_discovered") or [] if isinstance(k, str)}
    for key, entries in _sites_of(shipped).items():
        cur = sites.get(key)
        if cur is None or (legacy and _entry_urls(cur) & REPLACED_SAMPLES) or (not legacy and key not in found):
            sites[key] = entries
    out = {k: v for k, v in local.items() if k.startswith("_")}
    out.update({k: v for k, v in shipped.items() if k in ("_README", "_comment")})
    out["sites"] = sites
    return out


def sites_doc_for(path: Path, *, must_exist: bool = False) -> dict:
    """The sites document at ``path``; for the local file, overlaid with the shipped samples (overlay_shipped)."""
    if must_exist and not Path(path).exists():
        raise ProbeError(f"sites file not found: {path}")
    doc = read_sites_doc(path)
    if Path(path).resolve() == LOCAL_SITES.resolve() and DEFAULT_SITES.exists():
        doc = overlay_shipped(doc, read_sites_doc(DEFAULT_SITES))
    return doc


def _sites_of(doc: Any) -> dict:
    if not isinstance(doc, dict):
        return {}
    return doc.get("sites") if isinstance(doc.get("sites"), dict) else {k: v for k, v in doc.items() if not k.startswith("_")}


def merge_sites(raw: dict, results: list[StoreResult], *, when: dt.date | None = None) -> dict:
    """A new sites document: discovered URLs replace each found store's sample URLs.

    Entries that carry a ``retailer_config`` (Target pickup near a ZIP, Micro Center store 151, Best Buy
    delivery...) keep that config and their note, moved onto discovered URLs; stores discovery skipped or
    found nothing for keep their existing entries; every ``_`` key (``_README``...) is preserved,
    ``_comment`` records the run (after the samples' own comment) and ``_discovered`` lists the stores whose
    entries came from a discovery (see overlay_shipped)."""
    when = when or dt.date.today()
    doc = dict(raw) if isinstance(raw, dict) else {}
    sites = dict(_sites_of(doc))
    prev_found = {k for k in doc.get("_discovered") or [] if isinstance(k, str)}
    changed: list[str] = []
    for r in results:
        urls = [c["url"] for c in r.chosen]
        if r.skipped or not urls:
            continue
        old = sites.get(r.key) or []
        old = old if isinstance(old, list) else [old]
        cfg_entries = [e for e in old if _entry_cfg(e)]
        new: list[Any] = []
        for i in range(max(len(urls), len(cfg_entries))):
            url = urls[i % len(urls)]
            if i < len(cfg_entries):
                e = cfg_entries[i]
                item: dict[str, Any] = {"url": url, "retailer_config": _entry_cfg(e)}
                if e.get("note"):
                    item["note"] = e["note"]
                new.append(item)
            else:
                new.append(url)
        sites[r.key] = new
        changed.append(r.key)
    out = {k: v for k, v in doc.items() if k.startswith("_")}
    out["_discovered"] = sorted(prev_found | set(changed))
    prior = re.sub(r"^Discovered \S+ by `probe\.py discover`.*?keep their previous samples\.\s*", "",
                   str(doc.get("_comment") or ""), flags=re.S)
    out["_comment"] = (f"Discovered {when.isoformat()} by `probe.py discover` for {len(changed)} store(s): real product URLs "
               f"from sitemaps / the storefront, verified with the real checker (in-stock and sold-out mixed). "
               f"Stores it finds nothing for keep their previous samples." + (f" {prior}" if prior else ""))
    out["sites"] = sites
    return out


def dump_sites(doc: dict) -> str:
    """JSON like the hand-written sites.json: one store per block, one entry per line."""
    lines = ["{"]
    for k, v in doc.items():
        if k == "sites":
            continue
        lines.append(f"  {json.dumps(k)}: {json.dumps(v)},")
    lines.append('  "sites": {')
    items = list((doc.get("sites") or {}).items())
    for si, (key, entries) in enumerate(items):
        entries = entries if isinstance(entries, list) else [entries]
        lines.append(f"    {json.dumps(key)}: [")
        for ei, e in enumerate(entries):
            lines.append(f"      {json.dumps(e)}" + ("," if ei < len(entries) - 1 else ""))
        lines.append("    ]" + ("," if si < len(items) - 1 else ""))
    lines += ["  }", "}"]
    return "\n".join(lines) + "\n"


def read_sites_doc(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"sites": {}}
    except json.JSONDecodeError as e:
        raise ProbeError(f"{path} is not valid JSON: {e}") from e
    return data if isinstance(data, dict) else {"sites": {}}


async def discover_all(only: list[str] | None, sites_doc: dict, *, per_store: int = DEFAULT_PER_STORE,
                       concurrency: int = DEFAULT_DISCOVER_CONCURRENCY, max_tries: int = DEFAULT_MAX_TRIES,
                       time_cap: float = DEFAULT_STORE_TIMEOUT, base_config: dict | None = None, net: Any = None,
                       check: Callable[[str, dict | None], Any] | None = None,
                       on_result: Callable[[StoreResult], None] | None = None) -> list[StoreResult]:
    """Discover for the chosen registry stores, ``concurrency`` stores at a time."""
    _, _, registry = backend()
    known = {r.key: r for r in registry.RETAILERS}
    if only:
        unknown = [k for k in only if k not in known]
        if unknown:
            raise ProbeError(f"Unknown store(s): {', '.join(unknown)}. Known stores: {', '.join(sorted(known))}")
    retailers = [known[k] for k in only] if only else list(registry.RETAILERS)
    samples = load_sites_data(sites_doc)
    net = net or LiveNet()
    sem = asyncio.Semaphore(max(1, concurrency))
    results: list[StoreResult | None] = [None] * len(retailers)

    async def one(i: int, r: Any) -> None:
        async with sem:
            try:
                res = await discover_store(r, samples.get(r.key), net, per_store=per_store, max_tries=max_tries,
                                           time_cap=time_cap, check=check, base_config=base_config)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - one store must not stop the rest
                res = StoreResult(key=r.key, name=r.name, notes=[f"discovery crashed: {type(e).__name__}: {e}"])
            results[i] = res
            if on_result:
                on_result(res)

    await asyncio.gather(*(one(i, r) for i, r in enumerate(retailers)))
    return [r for r in results if r is not None]


# --------------------------------------------------------------------------- CLI


def config_from_options(*, zip_code: Any = None, fulfillment: Any = None, radius: Any = None,
                        store_id: Any = None, any_seller: bool = False) -> dict:
    rc: dict[str, Any] = {}
    if zip_code not in (None, ""):
        rc["zip"] = str(zip_code).strip()
    if fulfillment not in (None, ""):
        rc["fulfillment"] = str(fulfillment)
    if radius not in (None, ""):
        try:
            rc["radius_miles"] = int(radius)
        except (TypeError, ValueError) as e:
            raise ProbeError(f"radius must be a whole number of miles, got {radius!r}") from e
    if store_id not in (None, ""):
        rc["store_id"] = str(store_id).strip()
    if any_seller:
        rc["official_only"] = False
    return rc


def _apply_browser_flag(flag: bool | None) -> None:
    if flag is not None:
        os.environ["ENABLE_BROWSER"] = "true" if flag else "false"


def _chrome_installed() -> bool:
    with contextlib.suppress(Exception):
        _, fetcher, _ = backend()
        fn = getattr(fetcher, "chrome_installed", None)
        if callable(fn):
            return bool(fn())
    return False


def apply_browser_options(a: argparse.Namespace, *, platform: str | None = None) -> None:
    """Map the browser flags to the backend's environment variables.

    --headed / --headless -> BROWSER_MODE; --chrome / --no-chrome -> BROWSER_CHANNEL;
    --cdp URL -> BROWSER_CDP_URL. The probe keeps its own profile (.browser-profile/). On
    macOS the default is a headed, installed Google Chrome when there is one."""
    _apply_browser_flag(getattr(a, "browser", None))
    os.environ.setdefault("BROWSER_PROFILE_DIR", str(DEFAULT_PROFILE))
    mode = getattr(a, "browser_mode", None)
    if mode:
        os.environ["BROWSER_MODE"] = mode
    cdp = getattr(a, "cdp", None)
    if cdp:
        os.environ["BROWSER_CDP_URL"] = cdp
    chrome = getattr(a, "chrome", None)
    if chrome is True:
        os.environ["BROWSER_CHANNEL"] = "chrome"
    elif chrome is False:
        os.environ["BROWSER_CHANNEL"] = "chromium"
    elif (platform or sys.platform) == "darwin" and not os.environ.get("BROWSER_CHANNEL") and _chrome_installed():
        os.environ["BROWSER_CHANNEL"] = "chrome"


def _add_check_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--zip", help="ZIP code for pickup / local availability")
    p.add_argument("--fulfillment", choices=["delivery", "pickup", "any"])
    p.add_argument("--radius", type=int, help="pickup search radius in miles (default 25)")
    p.add_argument("--store-id", help="a specific store id (Micro Center, Target, ...)")
    p.add_argument("--any-seller", action="store_true", help="also count marketplace / third-party sellers")
    p.add_argument("--browser", action=argparse.BooleanOptionalAction, default=None,
                   help="allow/deny the browser fallback (default: the app's default, on)")
    _add_browser_opts(p)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output folder (default probe-output/)")


def _add_browser_opts(p: argparse.ArgumentParser) -> None:
    g = p.add_mutually_exclusive_group()
    g.add_argument("--headed", dest="browser_mode", action="store_const", const="headed",
                   help="show the browser window (default on macOS/Windows; Linux uses Xvfb when there is no screen)")
    g.add_argument("--headless", dest="browser_mode", action="store_const", const="headless",
                   help="run the browser without a window")
    p.add_argument("--chrome", action=argparse.BooleanOptionalAction, default=None,
                   help="use your installed Google Chrome instead of the bundled Chromium (default on macOS when installed)")
    p.add_argument("--cdp", metavar="URL",
                   help="use a Chrome you started with --remote-debugging-port, e.g. http://127.0.0.1:9222")


def _add_discover_opts(p: argparse.ArgumentParser, with_only: bool = True) -> None:
    p.add_argument("--per-store", type=int, default=DEFAULT_PER_STORE, metavar="N",
                   help=f"product URLs to keep per store (default {DEFAULT_PER_STORE}: one in stock, one sold out when possible)")
    p.add_argument("--max-tries", type=int, default=DEFAULT_MAX_TRIES, metavar="N",
                   help=f"candidates to check per store (default {DEFAULT_MAX_TRIES})")
    p.add_argument("--store-timeout", type=float, default=DEFAULT_STORE_TIMEOUT, metavar="SECONDS",
                   help=f"time cap per store (default {DEFAULT_STORE_TIMEOUT:.0f})")
    p.add_argument("--discover-concurrency", type=int, default=DEFAULT_DISCOVER_CONCURRENCY, metavar="N",
                   help=f"stores discovered at once (default {DEFAULT_DISCOVER_CONCURRENCY})")


def _cli_config(a: argparse.Namespace) -> dict:
    return config_from_options(zip_code=a.zip, fulfillment=a.fulfillment, radius=a.radius,
                               store_id=a.store_id, any_seller=a.any_seller)


async def _shutdown_backend() -> None:
    with contextlib.suppress(Exception):
        checkers, _, _ = backend()
        await checkers.shutdown()


def cmd_check(a: argparse.Namespace) -> int:
    apply_browser_options(a)
    rc = _cli_config(a)
    entries = [{"key": None, "url": u, "retailer_config": {}, "note": None} for u in a.urls]

    async def main() -> list[dict]:
        try:
            return await run_entries(entries, concurrency=a.concurrency, preview=a.preview, base_config=rc,
                                     out_root=a.out)
        finally:
            await _shutdown_backend()

    backend()  # fail early with a clear message
    results = asyncio.run(main())
    for s in results:
        print(format_summary(s))
        print()
    return 0 if all(s.get("verdict") in PASSING for s in results) else 1


def _discover_args(a: argparse.Namespace) -> dict:
    return {"per_store": a.per_store, "concurrency": a.discover_concurrency, "max_tries": a.max_tries,
            "time_cap": a.store_timeout}


def _print_discovery(results: list[StoreResult]) -> None:
    print("\n" + format_discovery(results))
    found = sum(1 for r in results if r.chosen)
    print(f"\n{found} of {len(results)} store(s) have discovered URLs "
          f"({sum(1 for r in results if r.skipped)} skipped on purpose).")


def cmd_discover(a: argparse.Namespace) -> int:
    apply_browser_options(a)
    rc = _cli_config(a)
    only = [k.strip() for k in (a.only or "").split(",") if k.strip()] or None
    if a.per_store < 1:
        raise ProbeError("--per-store must be at least 1")
    a.sites_arg = getattr(a, "sites_arg", getattr(a, "sites", None))
    a.sites = effective_sites(a.sites_arg)
    doc = sites_doc_for(a.sites)
    backend()
    print(f"Discovering up to {a.per_store} product URL(s) per store, {a.discover_concurrency} store(s) at a time "
          f"(<= {a.store_timeout:.0f}s each). This can take several minutes…")
    print(f"Browser: {describe_browser(browser_info())}")

    def on_result(r: StoreResult) -> None:
        got = f"{len(r.chosen)} URL(s)" if r.chosen else ("skipped" if r.skipped else "nothing usable")
        print(f"  {r.key:<14} {got}  [{r.source or '-'}, {r.found} found, {r.tried} checked]", flush=True)

    async def main() -> list[StoreResult]:
        try:
            return await discover_all(only, doc, base_config=rc, on_result=on_result, **_discover_args(a))
        finally:
            await _shutdown_backend()

    results = asyncio.run(main())
    _print_discovery(results)
    merged = merge_sites(doc, results)
    if a.write:
        dest = Path(a.sites_arg) if a.sites_arg is not None else LOCAL_SITES
        dest.write_text(dump_sites(merged), encoding="utf-8")
        print(f"Updated {_rel(dest)}. Next: ./run.sh sweep")
    else:
        Path(a.out).mkdir(parents=True, exist_ok=True)
        dest = Path(a.out) / "discovered.json"
        dest.write_text(dump_sites(merged), encoding="utf-8")
        print(f"Wrote {_rel(dest)} (sites.json format). Use --write to update {_rel(a.sites)}, or "
              f"`sweep --sites {_rel(dest)}` to try it first.")
    return 0


def cmd_sweep(a: argparse.Namespace) -> int:
    apply_browser_options(a)
    rc = _cli_config(a)
    only = [k.strip() for k in (a.only or "").split(",") if k.strip()] or None
    a.sites = effective_sites(a.sites)
    if a.discover:
        doc = sites_doc_for(a.sites)
        entries: list[dict] = []
    else:
        doc = {}
        entries = select_entries(load_sites_data(sites_doc_for(a.sites, must_exist=True)), only)
    backend()
    print(f"Browser: {describe_browser(browser_info())}")
    done = [0]
    total = [len(entries)]

    def on_result(_i: int, s: dict) -> None:
        done[0] += 1
        print(format_progress(done[0], total[0], s), flush=True)

    async def main() -> list[dict]:
        nonlocal entries
        try:
            if a.discover:
                print("Discovering current product URLs first…")
                found = await discover_all(only, doc, base_config=rc, **_discover_args(a))
                _print_discovery(found)
                merged = merge_sites(doc, found)
                Path(a.out).mkdir(parents=True, exist_ok=True)
                dest = Path(a.out) / "discovered.json"
                dest.write_text(dump_sites(merged), encoding="utf-8")
                print(f"(saved to {_rel(dest)}; sites.json is unchanged)")
                entries = select_entries(load_sites_data(merged), only)
                total[0] = len(entries)
            print(f"Checking {len(entries)} URL(s) with concurrency {a.concurrency}. This can take a few minutes…")
            return await run_entries(entries, concurrency=a.concurrency, preview=a.preview, base_config=rc,
                                     out_root=a.out, on_result=on_result)
        finally:
            await _shutdown_backend()

    results = asyncio.run(main())
    md, js = write_report(results, a.out, sites="probe-output/discovered.json (sweep --discover)" if a.discover
                          else a.sites)
    counts = {v: sum(1 for r in results if r.get("verdict") == v) for v in VERDICTS}
    print("\n" + ", ".join(f"{n} {v}" for v, n in counts.items()))
    print(f"Report: {_rel(md)}  (+ {_rel(js)})")
    print("Next: run `bundle` and send us the zip, or paste report.md into the chat / a GitHub issue.")
    return 0


def cmd_bundle(a: argparse.Namespace) -> int:
    zpath, included, skipped = make_bundle(a.out, a.dest, a.max_mb)
    size = zpath.stat().st_size / (1024 * 1024)
    print(f"Wrote {_rel(zpath)} ({size:.1f} MB, {len(included)} files"
          + (f", {len(skipped)} skipped over the {a.max_mb} MB cap" if skipped else "") + ").")
    print("Cookies and auth headers were stripped. Attach this zip in the chat or to a GitHub issue.")
    return 0


def cmd_serve(a: argparse.Namespace) -> int:
    apply_browser_options(a)
    backend()
    httpd, probe = start_server(a.port, a.out, effective_sites(a.sites), a.concurrency)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"Site probe UI: {url}  (only reachable from this computer; Ctrl+C to stop)")
    if a.open:
        import webbrowser

        with contextlib.suppress(Exception):
            webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping…")
    finally:
        httpd.server_close()
        probe.close()
    return 0


def cmd_fixture(a: argparse.Namespace) -> int:
    dest = make_fixture(a.bundle, a.name, a.dest, a.entry, a.force)
    print(f"Fixture '{a.name}' written to {_rel(dest)} (see index.json there).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="probe", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", metavar="command")

    c = sub.add_parser("check", help="check one or more product URLs and save recordings")
    c.add_argument("urls", nargs="+", metavar="URL")
    _add_check_opts(c)
    c.add_argument("--no-preview", dest="preview", action="store_false", help="skip the add-item preview fetch")
    c.add_argument("--concurrency", type=int, default=3)
    c.set_defaults(func=cmd_check, preview=True)

    s = sub.add_parser("sweep", help="check the sample URL(s) of every supported store and write a report")
    s.add_argument("--only", help="comma-separated store keys, e.g. target,bestbuy")
    s.add_argument("--sites", type=Path, default=None,
                   help="sites file (default sites.local.json if discover --write made one, else sites.json)")
    s.add_argument("--concurrency", type=int, default=3)
    s.add_argument("--preview", action="store_true", help="also run the add-item preview for each URL")
    s.add_argument("--discover", action="store_true",
                   help="first find current product URLs (like `discover`, without --write), then sweep those")
    _add_discover_opts(s, with_only=False)
    _add_check_opts(s)
    s.set_defaults(func=cmd_sweep)

    d = sub.add_parser("discover", help="find real, current product URLs for each store (sitemaps / storefront)")
    d.add_argument("--only", help="comma-separated store keys, e.g. target,bestbuy")
    d.add_argument("--sites", type=Path, default=None, dest="sites_arg",
                   help="sites file to read / update (default: sites.local.json if present, else sites.json)")
    d.add_argument("--write", action="store_true",
                   help="save the found URLs to sites.local.json (git-ignored; used by sweep from then on) "
                        "or to --sites; default: write probe-output/discovered.json")
    _add_discover_opts(d)
    _add_check_opts(d)
    d.set_defaults(func=cmd_discover)

    b = sub.add_parser("bundle", help="zip probe-output/ (cookies stripped) to share with the maintainers")
    b.add_argument("--out", type=Path, default=DEFAULT_OUT, help="folder to zip (default probe-output/)")
    b.add_argument("--dest", type=Path, default=HERE, help="where to write the zip")
    b.add_argument("--max-mb", type=float, default=DEFAULT_CAP_MB, help=f"size cap in MB (default {DEFAULT_CAP_MB})")
    b.set_defaults(func=cmd_bundle)

    v = sub.add_parser("serve", help="local web UI on 127.0.0.1")
    v.add_argument("--port", type=int, default=8765)
    v.add_argument("--sites", type=Path, default=None)
    v.add_argument("--concurrency", type=int, default=3)
    v.add_argument("--out", type=Path, default=DEFAULT_OUT)
    v.add_argument("--browser", action=argparse.BooleanOptionalAction, default=None)
    _add_browser_opts(v)
    v.add_argument("--open", action=argparse.BooleanOptionalAction, default=True, help="open it in your browser")
    v.set_defaults(func=cmd_serve)

    f = sub.add_parser("fixture", help="(maintainers) turn a recorded check into test fixtures")
    f.add_argument("bundle", type=Path, help="a probe-output/<check> folder or a probe-results zip")
    f.add_argument("--name", required=True, help="fixture name, e.g. target_in_stock")
    f.add_argument("--entry", help="which check folder inside a zip")
    f.add_argument("--dest", type=Path, default=LIVE_FIXTURES, help="fixtures root (default backend/tests/checkers/fixtures/live)")
    f.add_argument("--force", action="store_true", help="replace an existing fixture with the same name")
    f.set_defaults(func=cmd_fixture)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(errors="replace")  # Windows consoles
    parser = build_parser()
    a = parser.parse_args(argv)
    if not getattr(a, "func", None):
        parser.print_help()
        return 0
    try:
        return a.func(a)
    except ProbeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
