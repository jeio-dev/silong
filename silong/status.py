"""Phone URL/QR, hub status counts and measured model availability.

Everything here is pure or lazy: qrcode, httpx and the Ollama resolver are
imported only when a feature that needs them runs, so `import silong.config`
and `import silong.app` still load no optional or model dependencies.
"""

import asyncio
import ipaddress
from datetime import datetime, timezone
from typing import Iterable, Optional

from urllib.parse import urlsplit

DEFAULT_HTTP_PORT = 80
URGENCY_LEVELS = ("critical", "high", "medium", "low")
# Phoneless statuses that workers can set, counted separately from the queue.
PROCESSING_STATUSES = ("transcribing", "extracting")
QR_UNAVAILABLE = (
    "No phone address is available. Open this board using the hub's Wi-Fi IPv4 "
    "address (for example http://192.168.1.20:PORT/board) or set SILONG_HUB_URL "
    "to that address, then the phones' address and QR code appear here."
)


class PhoneUrlError(ValueError):
    """A configured address cannot be presented to phones."""


def _eligible_host(hostname: str) -> bool:
    """True for a numeric, non-loopback IPv4 host a phone on the LAN could reach."""
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    if not isinstance(address, ipaddress.IPv4Address):
        return False
    return not (
        address.is_loopback
        or address.is_unspecified
        or address.is_multicast
        or address.is_reserved
        or address.is_link_local
    )


def normalize_hub_url(raw: str) -> str:
    """Normalize `raw` to a phone-safe `http://host[:port]/` origin, or raise
    PhoneUrlError. Only numeric, non-loopback IPv4 hosts are accepted; the URL
    has no credentials, query, fragment or non-root path, and uses plain HTTP
    (default port 80 is omitted from the result)."""
    raw = raw.strip()
    if not raw:
        raise PhoneUrlError("SILONG_HUB_URL must not be blank.")
    candidate = raw if "://" in raw else f"http://{raw}"
    try:
        parts = urlsplit(candidate)
    except ValueError:
        raise PhoneUrlError(f"SILONG_HUB_URL is not a usable address: {raw!r}") from None
    if parts.scheme != "http":
        raise PhoneUrlError("SILONG_HUB_URL must be a plain http:// address, not https://.")
    if parts.username is not None or parts.password is not None:
        raise PhoneUrlError("SILONG_HUB_URL must not include a username or password.")
    if parts.query or parts.fragment:
        raise PhoneUrlError("SILONG_HUB_URL must not include a query or fragment.")
    if parts.path not in ("", "/"):
        raise PhoneUrlError("SILONG_HUB_URL must be the hub origin without a path.")
    hostname = parts.hostname
    if not hostname or not _eligible_host(hostname):
        raise PhoneUrlError(
            "SILONG_HUB_URL must use a numeric, non-loopback IPv4 address"
            " (no hostnames, wildcards, or 127.x/localhost addresses)."
        )
    try:
        port = parts.port
    except ValueError:
        raise PhoneUrlError("SILONG_HUB_URL has an invalid port.") from None
    if port is None:
        port = DEFAULT_HTTP_PORT
    if not 1 <= port <= 65535:
        raise PhoneUrlError("SILONG_HUB_URL has an invalid port.")
    if port == DEFAULT_HTTP_PORT:
        return f"http://{hostname}/"
    return f"http://{hostname}:{port}/"


def resolve_phone_url(config, host_header: str) -> Optional[str]:
    """The URL to advertise to phones, or None when none is available.

    With `config.hub_url` (a validated override) that is returned verbatim.
    Otherwise the request's Host header is used, but only when its host is an
    eligible numeric IPv4 address; localhost, 0.0.0.0 and hostnames never reach
    phones, so they yield no URL (and so no QR).
    """
    if config.hub_url:
        return config.hub_url
    header = (host_header or "").strip()
    if not header:
        return None
    try:
        return normalize_hub_url(header)
    except PhoneUrlError:
        return None


def local_date_of(created_at: str, tz=None) -> str:
    """The hub-local calendar date (YYYY-MM-DD) of a UTC ISO `created_at`.

    `tz` defaults to the hub's local timezone; pass one for deterministic tests.
    """
    parsed = datetime.fromisoformat(created_at)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(tz).date().isoformat()


def today_local_date(now: Optional[datetime] = None) -> str:
    """The hub's local calendar date for `now` (defaults to the present)."""
    return (now or datetime.now(timezone.utc)).astimezone().date().isoformat()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def build_status(
    rows: Iterable[dict],
    *,
    hub_url: Optional[str],
    model: Optional[str],
    model_state: str,
    model_checked_at: Optional[str],
    worker_running: bool,
    audio_available: bool = False,
    local_date: Optional[str] = None,
    snapshot_at: Optional[str] = None,
) -> dict:
    """Assemble the `/api/status` payload from one consistent `rows` snapshot.

    All counts come from `rows`. `reports_today` counts every row whose receipt
    falls on the hub's local calendar date (resolved and failed included);
    `queue_length` counts `queued` rows and `processing_count` counts
    `transcribing`/`extracting`; `urgency_counts` counts unresolved classified
    rows only. Queued or failed rows are never counted as an urgency.
    """
    if local_date is None:
        local_date = today_local_date()
    if snapshot_at is None:
        snapshot_at = _utc_now()
    queue_length = 0
    processing_count = 0
    reports_today = 0
    urgency_counts = {level: 0 for level in URGENCY_LEVELS}
    for row in rows:
        status = row["status"]
        if status == "queued":
            queue_length += 1
        if status in PROCESSING_STATUSES:
            processing_count += 1
        if local_date_of(row["created_at"]) == local_date:
            reports_today += 1
        if not row["resolved"] and row["urgency"] in urgency_counts:
            urgency_counts[row["urgency"]] += 1
    return {
        "hub_url": hub_url,
        "model": model,
        "model_state": model_state,
        "model_checked_at": model_checked_at,
        "worker_running": worker_running,
        "audio_available": audio_available,
        "queue_length": queue_length,
        "processing_count": processing_count,
        "reports_today": reports_today,
        "local_date": local_date,
        "urgency_counts": urgency_counts,
        "snapshot_at": snapshot_at,
    }


def _normalize_tag(tag: str) -> str:
    tag = tag.strip()
    return tag if ":" in tag else f"{tag}:latest"


async def check_model(
    model: Optional[str], *, transport=None, base_url: Optional[str] = None, timeout: float = 1.0
):
    """Return `(model_state, checked_at)` for `model`, probing Ollama's local
    `/api/ps` with a total wall-clock bound of `timeout` seconds.

    No model returns `not_configured` without importing or probing anything.
    Otherwise an exact loaded-tag match (a missing tag is normalized to
    `:latest`) gives `loaded`, a valid response without that model gives
    `not_loaded`, and a timeout, bad JSON/schema or HTTP failure gives
    `unavailable`. No generate/chat/load request is ever sent.
    """
    if not model:
        return ("not_configured", None)
    checked_at = _utc_now()
    if base_url is None:
        from silong import extract  # lazy: loads httpx only when a model exists

        base_url = extract.ollama_url()
    base_url = base_url.rstrip("/")
    import httpx  # lazy too

    try:
        async with httpx.AsyncClient(
            base_url=base_url, transport=transport, timeout=timeout
        ) as client:
            try:
                response = await asyncio.wait_for(client.get("/api/ps"), timeout=timeout)
            except (httpx.HTTPError, TimeoutError):
                return ("unavailable", checked_at)
    except (httpx.HTTPError, ValueError) as error:
        return ("unavailable", checked_at)
    if response.status_code >= 400:
        return ("unavailable", checked_at)
    try:
        body = response.json()
    except ValueError:
        return ("unavailable", checked_at)
    if not isinstance(body, dict):
        return ("unavailable", checked_at)
    models = body.get("models")
    if not isinstance(models, list):
        return ("unavailable", checked_at)
    want = _normalize_tag(model)
    for entry in models:
        if isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str) and _normalize_tag(name) == want:
                return ("loaded", checked_at)
    return ("not_loaded", checked_at)


def make_qr_svg(url: str) -> bytes:
    """Encode `url` as a local, self-contained SVG QR (no remote assets)."""
    import qrcode
    import qrcode.image.svg

    image = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage)
    return image.to_string()