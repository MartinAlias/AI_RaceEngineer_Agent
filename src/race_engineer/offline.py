"""Connectivity check, so a dead paddock connection is detected in seconds, not after SDK retries."""

from __future__ import annotations

import os
import socket
from urllib.parse import urlparse


def is_online(timeout: float = 3.0) -> bool:
    base = os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"
    url = urlparse(base)
    host = url.hostname or "api.anthropic.com"
    port = url.port or (80 if url.scheme == "http" else 443)
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False
