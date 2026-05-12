"""URL classification and routing for the Video Intelligence Bot."""

import ipaddress
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PRIVATE_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0"}


def _is_private_ip(host: str) -> bool:
    """Return True if *host* is a numeric private / loopback IP address."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # host is a domain name — not a bare IP, so it is fine
        return False
    return addr.is_private or addr.is_loopback or addr.is_unspecified


def _classify_youtube(host: str) -> bool:
    """Return True if the host belongs to YouTube (long-form)."""
    clean = host.lower().removeprefix("www.")
    return clean in {"youtube.com", "youtu.be"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_url(text: str) -> dict:
    """Parse *text* and return routing metadata.

    Accepted forms
    --------------
    - A plain URL: ``https://youtu.be/abc``
    - A /refresh command: ``/refresh https://youtu.be/abc``

    Returns
    -------
    ``{"type": "long"|"short", "url": str, "force": bool}``

    Raises
    ------
    ValueError
        With a user-friendly message when input is invalid or unsafe.
    """
    text = text.strip()
    force = False

    if text.startswith("/refresh"):
        remainder = text[len("/refresh"):].strip()
        if not remainder:
            raise ValueError(
                "Usage: /refresh <url>  — please provide a URL after /refresh."
            )
        text = remainder
        force = True

    parsed = urlparse(text)

    # Must have a non-empty scheme and netloc
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(
            f"Invalid URL: {text!r}. Make sure it starts with https:// or http://."
        )

    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"Unsupported scheme {parsed.scheme!r}. Only http and https are allowed."
        )

    host = parsed.hostname or ""

    if host in _PRIVATE_HOSTS or _is_private_ip(host):
        raise ValueError(
            f"URL host {host!r} resolves to a private or loopback address, "
            "which is not allowed."
        )

    url_type = "long" if _classify_youtube(host) else "short"
    return {"type": url_type, "url": text, "force": force}
