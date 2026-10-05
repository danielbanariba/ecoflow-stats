"""Unit tests for the container's own healthcheck probe: a tiny
synchronous HTTP client hitting this same process's `/healthz`, so
Docker's `HEALTHCHECK` directive needs no extra tooling (no curl) in the
runtime image. `get` is always injected here -- no test opens a real
socket, even one that would just be refused instantly.
"""

from __future__ import annotations

import io
import os

import httpx
import pytest

from ecoflow_stats.config import load_settings
from ecoflow_stats.healthcheck import run_healthcheck

VALID_ENV = {
    "ECOFLOW_ACCESS_KEY": "test-access-key",
    "ECOFLOW_SECRET_KEY": "test-secret-key",
    "ECOFLOW_DEVICES": "TESTDEV0001",
    "ECOFLOW_STATS_PORT": "9999",
}


@pytest.fixture(autouse=True)
def _clean_ecoflow_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("ECOFLOW_"):
            monkeypatch.delenv(name, raising=False)


def _settings(monkeypatch: pytest.MonkeyPatch):
    for key, value in VALID_ENV.items():
        monkeypatch.setenv(key, value)
    return load_settings(os.environ)


def test_probes_127_0_0_1_on_the_configured_port_and_healthz_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    seen = []

    def fake_get(url: str) -> httpx.Response:
        seen.append(url)
        return httpx.Response(200, json={"status": "ok", "devices": []})

    run_healthcheck(settings, get=fake_get)

    assert seen == ["http://127.0.0.1:9999/healthz"]


def test_exits_0_and_prints_the_status_and_each_devices_age_when_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    response = httpx.Response(
        200, json={"status": "ok", "devices": [{"id": 1, "last_sample_age_s": 42}]}
    )
    out = io.StringIO()
    exit_code = run_healthcheck(settings, get=lambda url: response, out=out)

    assert exit_code == 0
    text = out.getvalue()
    assert "ok" in text
    assert "42" in text


def test_names_a_device_with_no_sample_yet_instead_of_printing_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    response = httpx.Response(
        200, json={"status": "starting", "devices": [{"id": 1, "last_sample_age_s": None}]}
    )
    out = io.StringIO()

    exit_code = run_healthcheck(settings, get=lambda url: response, out=out)

    assert exit_code == 0
    assert "no sample yet" in out.getvalue()
    assert "None" not in out.getvalue()


def test_exits_1_on_a_503_response(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    response = httpx.Response(503, json={"status": "degraded", "devices": []})
    out = io.StringIO()

    exit_code = run_healthcheck(settings, get=lambda url: response, out=out)

    assert exit_code == 1
    assert "503" in out.getvalue()


def test_exits_1_when_the_server_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    out = io.StringIO()

    def fake_get(url: str) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    exit_code = run_healthcheck(settings, get=fake_get, out=out)

    assert exit_code == 1
    assert "unreachable" in out.getvalue()
