"""Signed upload URLs for image inputs.

Passing an image as base64 routes every byte through the model's tool-call
arguments. Instead, a client asks for an upload URL, sends the raw file there
(e.g. ``curl -T photo.jpg <url>``) and passes the returned ``input:<filename>``
reference to the workflow. The URL carries its own short-lived HMAC token, so
the caller's shell never needs the server's bearer token.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time

logger = logging.getLogger(__name__)

_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
    (b"II*\x00", ".tiff"),
    (b"MM\x00*", ".tiff"),
)


def image_suffix(data: bytes) -> str | None:
    """The file extension matching the image format in ``data``, or None."""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    for magic, suffix in _SIGNATURES:
        if data.startswith(magic):
            return suffix
    return None


class UploadSigner:
    def __init__(self, secret: str, ttl: float):
        if not secret:
            # Tokens then only verify on the replica that issued them.
            logger.info("no UPLOAD_SECRET or API_TOKEN set; upload URLs are signed with a per-process key")
        self._key = secret.encode() if secret else secrets.token_bytes(32)
        self.ttl = ttl

    def issue(self) -> tuple[str, int]:
        """Return a fresh token and its expiry as a Unix timestamp."""
        expires = int(time.time() + self.ttl)
        payload = f"{expires}.{secrets.token_hex(8)}"
        return f"{payload}.{self._sign(payload)}", expires

    def verify(self, token: str) -> str | None:
        """Return the token's nonce if it is authentic and unexpired."""
        expires, _, rest = token.partition(".")
        nonce, _, signature = rest.partition(".")
        if not (expires.isdigit() and nonce and signature):
            return None
        if not hmac.compare_digest(signature, self._sign(f"{expires}.{nonce}")):
            return None
        if int(expires) < time.time():
            return None
        return nonce

    def _sign(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()
