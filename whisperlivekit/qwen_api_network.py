"""Network policy shared by the DashScope Qwen API clients."""

from __future__ import annotations

import os
from urllib.parse import urlsplit


def ensure_direct_connection(url: str) -> str:
    """Add ``url``'s host to NO_PROXY without replacing existing entries.

    macOS exposes its system HTTP/SOCKS proxy to ``urllib``. DashScope's
    realtime request pattern is sensitive to intermittent local-proxy TLS
    failures, so Qwen API traffic is direct by default while every unrelated
    destination retains the user's normal proxy policy.
    """

    host = (urlsplit(url).hostname or "").strip().lower()
    if not host:
        return ""

    combined: list[str] = []
    seen: set[str] = set()
    for variable in ("NO_PROXY", "no_proxy"):
        for entry in os.getenv(variable, "").split(","):
            value = entry.strip()
            key = value.casefold()
            if value and key not in seen:
                seen.add(key)
                combined.append(value)
    if host.casefold() not in seen:
        combined.append(host)

    value = ",".join(combined)
    os.environ["NO_PROXY"] = value
    os.environ["no_proxy"] = value
    return host
