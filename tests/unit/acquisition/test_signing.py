"""Unit tests for EcoFlow cloud request signing.

Covers the acquisition "Requests Are Authenticated and Accepted by the Live
API" requirement's signing half, and the verified GET quirk documented in
``ecoflow-panel/docs/API.md``: on a GET request the live API rejects a
signed string that includes query parameters (``8521 signature is wrong``),
so the signed string must contain only ``accessKey``, ``nonce`` and
``timestamp`` — never any request parameter such as ``sn``.
"""

from __future__ import annotations

from ecoflow_stats.acquisition.signing import sign


def test_sign_matches_known_vector_a() -> None:
    """A fixed secret key, access key, nonce and timestamp must always
    produce this exact pinned signature. A wrong HMAC algorithm, a wrong
    signed-string format, or swapping which key is the HMAC secret each
    change this value."""
    produced = sign(
        secret_key="s3cr3t-signing-key",
        access_key="AKIDEXAMPLE1234",
        nonce="482910",
        timestamp_ms="1733400000000",
    )
    assert produced == "59167a3a8a7adce23df7e1df849e828759a3e17eb195acf5993f0ca7f124a633"


def test_sign_matches_known_vector_b() -> None:
    """Triangulation: a second, unrelated set of inputs must produce a
    different, independently pinned signature. This forces a real HMAC
    computation — a hardcoded return value from the first test would fail
    here, since the expected output differs."""
    produced = sign(
        secret_key="another-secret-9f",
        access_key="AK-SECOND-7788",
        nonce="007321",
        timestamp_ms="1700000500123",
    )
    assert produced == "3400f8001b0126ddc24106f464a013ebc1f26b612d59db14afb1c486377acf75"


def test_sign_excludes_query_parameters_from_signed_string() -> None:
    """Regression test for the verified GET quirk: EcoFlow's published
    signing scheme flattens and sorts request parameters, then appends
    ``accessKey``/``nonce``/``timestamp`` — e.g. for a quota request it
    would sign ``sn=<serial>&accessKey=...&nonce=...&timestamp=...``. The
    live API rejects exactly that string on GET with "signature is wrong";
    only the no-params form is accepted. This pins the signature that
    WOULD result from signing the documented-but-wrong, params-included
    string, and proves ``sign()`` produces neither that value nor any
    value that depends on a device serial — only the three credentials.
    """
    produced = sign(
        secret_key="s3cr3t-signing-key",
        access_key="AKIDEXAMPLE1234",
        nonce="482910",
        timestamp_ms="1733400000000",
    )
    signed_with_serial_included = "cc0b3177900ef146dcd8a294a70b3ca6984354d1937c179c3a46ac75893d398c"
    assert produced != signed_with_serial_included
    assert produced == "59167a3a8a7adce23df7e1df849e828759a3e17eb195acf5993f0ca7f124a633"
