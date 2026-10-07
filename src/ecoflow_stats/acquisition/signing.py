"""Pure HMAC-SHA256 request signing for the EcoFlow IoT Open API.

EcoFlow's published scheme flattens and sorts every request parameter, then
appends ``accessKey``, ``nonce`` and ``timestamp`` in that order, before
HMAC-SHA256-signing the result. Verified against the live API
(``ecoflow-panel/docs/API.md``), that documented scheme is wrong for a GET
request: including the request's own query parameters (for example ``sn``)
makes the API reject the request with ``8521 signature is wrong``. Only the
three credentials are signed. ``sign`` therefore takes no parameters
argument at all — there is no way to pass a query parameter into it, so this
module cannot regress into reproducing the documented-but-wrong scheme.
"""

from __future__ import annotations

import hmac
from hashlib import sha256


def sign(*, secret_key: str, access_key: str, nonce: str, timestamp_ms: str) -> str:
    """Return the hex HMAC-SHA256 signature for one EcoFlow cloud request.

    Signs exactly ``accessKey=<access_key>&nonce=<nonce>&timestamp=<timestamp_ms>``
    with ``secret_key``, matching the live API's accepted GET-signing form.
    """
    plain = f"accessKey={access_key}&nonce={nonce}&timestamp={timestamp_ms}"
    return hmac.new(secret_key.encode(), plain.encode(), sha256).hexdigest()


__all__ = ["sign"]
