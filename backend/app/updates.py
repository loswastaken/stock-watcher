"""Version check against the published image, and "Update now" via Watchtower's HTTP API."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from .config import get_settings

log = logging.getLogger("stockwatcher.updates")

REVISION_LABEL = "org.opencontainers.image.revision"
CACHE_SECONDS = 300
_ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))


class UpdateError(Exception):
    pass


@dataclass
class LatestImage:
    revision: str | None
    created: str | None


def split_image(ref: str) -> tuple[str, str, str]:
    """'ghcr.io/owner/name:tag' -> ('ghcr.io', 'owner/name', 'tag')."""
    registry, _, rest = ref.partition("/")
    repo, _, tag = rest.partition(":")
    return registry, repo, tag or "latest"


def _pick_manifest(index: dict) -> str | None:
    manifests = [m for m in index.get("manifests") or [] if isinstance(m, dict)]
    real = [m for m in manifests if (m.get("platform") or {}).get("os") not in (None, "unknown")]
    for m in real or manifests:
        if (m.get("platform") or {}).get("architecture") == "amd64":
            return m.get("digest")
    return (real or manifests or [{}])[0].get("digest")


async def fetch_latest(client: httpx.AsyncClient | None = None) -> LatestImage:
    """Read the git revision baked into the published image's labels (anonymous pull)."""
    registry, repo, tag = split_image(get_settings().update_image)
    own = client is None
    client = client or httpx.AsyncClient(timeout=15, follow_redirects=True)
    try:
        r = await client.get(f"https://{registry}/token", params={"scope": f"repository:{repo}:pull"})
        if r.status_code != 200:
            raise UpdateError(f"{registry} refused anonymous access (HTTP {r.status_code}); is the package public?")
        headers = {"Authorization": f"Bearer {r.json().get('token', '')}", "Accept": _ACCEPT}
        base = f"https://{registry}/v2/{repo}"
        r = await client.get(f"{base}/manifests/{tag}", headers=headers)
        if r.status_code != 200:
            raise UpdateError(f"Could not read {repo}:{tag} (HTTP {r.status_code})")
        doc = r.json()
        if "manifests" in doc:  # multi-arch index -> one platform manifest
            digest = _pick_manifest(doc)
            r = await client.get(f"{base}/manifests/{digest}", headers=headers)
            if r.status_code != 200:
                raise UpdateError(f"Could not read image manifest (HTTP {r.status_code})")
            doc = r.json()
        cfg_digest = (doc.get("config") or {}).get("digest")
        r = await client.get(f"{base}/blobs/{cfg_digest}", headers=headers)
        if r.status_code != 200:
            raise UpdateError(f"Could not read image config (HTTP {r.status_code})")
        cfg = r.json()
        labels = (cfg.get("config") or {}).get("Labels") or {}
        return LatestImage(revision=labels.get(REVISION_LABEL), created=cfg.get("created"))
    except httpx.HTTPError as e:
        raise UpdateError(f"Could not reach {registry}: {type(e).__name__}") from e
    finally:
        if own:
            await client.aclose()


_cache: dict = {"at": None, "latest": None, "error": None}  # at: monotonic time of last check


def _same(a: str | None, b: str | None) -> bool:
    return bool(a and b) and (a.startswith(b) or b.startswith(a))


async def status(force: bool = False) -> dict:
    s = get_settings()
    if force or _cache["at"] is None or time.monotonic() - _cache["at"] > CACHE_SECONDS:
        try:
            _cache["latest"], _cache["error"] = await fetch_latest(), None
        except UpdateError as e:
            _cache["error"] = str(e)
        _cache["at"] = time.monotonic()
    latest: LatestImage | None = _cache["latest"]
    current = s.app_version
    rev = latest.revision if latest else None
    return {
        "current_version": current,
        "latest_version": rev,
        "latest_created": latest.created if latest else None,
        "update_available": bool(rev) and current != "dev" and not _same(current, rev),
        "can_update": bool(s.watchtower_url and s.watchtower_token),
        "error": _cache["error"],
    }


async def trigger_update(client: httpx.AsyncClient | None = None) -> None:
    """Ask Watchtower to update this image. Watchtower restarts us while the request is open,
    so a timeout / dropped connection after it was accepted means the update is under way."""
    s = get_settings()
    if not (s.watchtower_url and s.watchtower_token):
        raise UpdateError("Set WATCHTOWER_URL and WATCHTOWER_TOKEN to update from the app")
    image = s.update_image.rsplit(":", 1)[0]
    own = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=5.0))
    try:
        r = await client.get(f"{s.watchtower_url.rstrip('/')}/v1/update", params={"image": image},
                             headers={"Authorization": f"Bearer {s.watchtower_token}"})
        if r.status_code in (401, 403):
            raise UpdateError("Watchtower rejected the token (WATCHTOWER_TOKEN)")
        if r.status_code >= 400:
            raise UpdateError(f"Watchtower returned HTTP {r.status_code}")
    except (httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.ReadError):
        log.info("watchtower update request still running / connection dropped: update in progress")
    except httpx.HTTPError as e:
        raise UpdateError(f"Could not reach Watchtower at {s.watchtower_url}: {type(e).__name__}") from e
    finally:
        if own:
            await client.aclose()
    _cache["at"] = None  # re-check after an update
