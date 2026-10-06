"""Fail-fast environment configuration.

``load_settings`` is the application's single configuration gate: every
required or malformed environment variable is collected and reported
together, naming each failing variable, rather than stopping at the first
problem. Nothing here touches the filesystem beyond reading an explicit
``<NAME>_FILE`` secret path and checking whether ``ECOFLOW_STATS_DATA_DIR``
already exists as a non-directory — it never creates a directory itself
(that happens in the composition root, so this module stays pure and easy
to unit test).
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    from collections.abc import Mapping

_SERIAL_RE = re.compile(r"^[A-Z0-9]{8,32}$")
_ADAPTER_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SUPPORTED_LANGS = ("en", "es")
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_DEFAULT_ALLOWED_NETWORKS = (
    "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,"
    "100.64.0.0/10,169.254.0.0/16,fc00::/7,fe80::/10"
)
# The three secrets an operator is most likely to inject as a Docker secret
# file rather than a plain environment variable: the two cloud credentials
# and the local web password.
_FILE_FALLBACK_SECRETS = ("ECOFLOW_ACCESS_KEY", "ECOFLOW_SECRET_KEY", "ECOFLOW_STATS_PASSWORD")


class ConfigError(Exception):
    """Raised when one or more environment variables fail validation.

    Carries every failing variable's name and reason, not just the first
    one found, so a misconfigured deployment is fixed in a single pass.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(errors))


@dataclass(frozen=True, slots=True)
class DeviceConfig:
    """One configured EcoFlow device, parsed from ``ECOFLOW_DEVICES``."""

    serial: str
    adapter_id: str | None


@dataclass(frozen=True, slots=True)
class PublicSettings:
    """Non-secret view of :class:`Settings`, safe to hand to a template.

    Grown incrementally: later phases add the fields their pages need.
    Every field here is deliberately not one of ``Settings``'s secrets.
    """

    api_host: str
    tz: str
    poll_interval: int
    default_lang: str
    notify_lang: str
    currency: str
    tariff: float | None
    session_days: int
    log_level: str


@dataclass(frozen=True, slots=True)
class Settings:
    """Fully validated application configuration."""

    access_key: str
    secret_key: str
    devices: tuple[DeviceConfig, ...]
    api_host: str
    data_dir: Path
    tz: str
    poll_interval: int
    poll_offset: int
    outage_threshold_v: float
    gap_threshold: int
    stale_threshold: int
    tariff: float | None
    currency: str
    ntfy_topic: str | None
    ntfy_url: str
    ntfy_token: str | None
    notify_lang: str
    default_lang: str
    password: str | None
    allowed_networks: tuple[str, ...]
    trusted_proxies: tuple[str, ...]
    session_days: int
    host: str
    port: int
    log_level: str

    def to_public(self) -> PublicSettings:
        """Project this configuration onto its template-safe subset."""
        return PublicSettings(
            api_host=self.api_host,
            tz=self.tz,
            poll_interval=self.poll_interval,
            default_lang=self.default_lang,
            notify_lang=self.notify_lang,
            currency=self.currency,
            tariff=self.tariff,
            session_days=self.session_days,
            log_level=self.log_level,
        )


class _Loader:
    """Accumulates validation errors across one ``load_settings`` call."""

    def __init__(self, environ: Mapping[str, str]) -> None:
        self._environ = environ
        self.errors: list[str] = []

    def _raw(self, name: str) -> str | None:
        value = self._environ.get(name)
        return value if value else None

    def require_secret(self, name: str) -> str | None:
        value = self._raw(name)
        if value is not None:
            return value
        if name in _FILE_FALLBACK_SECRETS:
            file_value = self._read_secret_file(name)
            if file_value is not None:
                return file_value
        self.errors.append(f"{name}: required variable is missing")
        return None

    def optional_secret(self, name: str) -> str | None:
        value = self._raw(name)
        if value is not None:
            return value
        if name in _FILE_FALLBACK_SECRETS:
            return self._read_secret_file(name)
        return None

    def _read_secret_file(self, name: str) -> str | None:
        path_value = self._raw(f"{name}_FILE")
        if path_value is None:
            return None
        try:
            return Path(path_value).read_text().strip()
        except OSError as exc:
            self.errors.append(f"{name}_FILE: could not read {path_value!r} ({exc})")
            return None

    def string(self, name: str, default: str) -> str:
        return self._raw(name) or default

    def int_in_range(self, name: str, default: int, low: int, high: int) -> int:
        raw = self._raw(name)
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError:
            self.errors.append(f"{name}: {raw!r} is not an integer")
            return default
        if not (low <= value <= high):
            self.errors.append(f"{name}: {value} is outside the allowed range [{low}, {high}]")
            return default
        return value

    def float_in_range(self, name: str, default: float, low: float, high: float) -> float:
        raw = self._raw(name)
        if raw is None:
            return default
        try:
            value = float(raw)
        except ValueError:
            self.errors.append(f"{name}: {raw!r} is not a number")
            return default
        if not (low <= value <= high):
            self.errors.append(f"{name}: {value} is outside the allowed range [{low}, {high}]")
            return default
        return value

    def optional_nonneg_decimal(self, name: str) -> float | None:
        raw = self._raw(name)
        if raw is None:
            return None
        try:
            value = float(raw)
        except ValueError:
            self.errors.append(f"{name}: {raw!r} is not a number")
            return None
        if value < 0:
            self.errors.append(f"{name}: {value} must not be negative")
            return None
        return value

    def enum(self, name: str, default: str, choices: tuple[str, ...]) -> str:
        raw = self._raw(name)
        if raw is None:
            return default
        if raw not in choices:
            self.errors.append(f"{name}: {raw!r} is not one of {choices}")
            return default
        return raw

    def timezone(self, name: str, default: str) -> str:
        raw = self._raw(name)
        value = raw or default
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError, ValueError:
            self.errors.append(f"{name}: {value!r} is not a known IANA timezone")
            return default
        return value

    def data_dir(self, name: str, default: str) -> Path:
        raw = self._raw(name)
        value = Path(raw or default)
        if value.exists() and not value.is_dir():
            self.errors.append(f"{name}: {value} exists and is not a directory")
        return value

    def http_url(self, name: str, default: str, schemes: tuple[str, ...]) -> str:
        raw = self._raw(name)
        value = raw or default
        parsed = urlsplit(value)
        if parsed.scheme not in schemes or not parsed.netloc:
            self.errors.append(f"{name}: {value!r} is not a valid {'/'.join(schemes)} URL")
            return default
        return value

    def ecoflow_api_host(self, name: str, default: str) -> str:
        raw = self._raw(name)
        value = raw or default
        parsed = urlsplit(value)
        host = parsed.netloc
        valid = parsed.scheme == "https" and (
            host == "ecoflow.com" or host.endswith(".ecoflow.com")
        )
        if not valid:
            self.errors.append(f"{name}: {value!r} must be an https URL on *.ecoflow.com")
            return default
        return value

    def cidr_list(self, name: str, default: str) -> tuple[str, ...]:
        raw = self._raw(name) or default
        entries = tuple(e.strip() for e in raw.split(",") if e.strip())
        for entry in entries:
            try:
                ipaddress.ip_network(entry, strict=False)
            except ValueError:
                self.errors.append(f"{name}: {entry!r} is not a valid CIDR network")
        return entries


def _parse_devices(loader: _Loader, name: str) -> tuple[DeviceConfig, ...]:
    raw = loader._raw(name)
    if raw is None:
        loader.errors.append(f"{name}: required variable is missing")
        return ()
    devices: list[DeviceConfig] = []
    for entry in (e.strip() for e in raw.split(",") if e.strip()):
        serial, _, adapter_id = entry.partition(":")
        if not _SERIAL_RE.match(serial):
            loader.errors.append(f"{name}: {entry!r} has an invalid device serial")
            continue
        if adapter_id and not _ADAPTER_ID_RE.match(adapter_id):
            loader.errors.append(f"{name}: {entry!r} has an invalid adapter id")
            continue
        devices.append(DeviceConfig(serial=serial, adapter_id=adapter_id or None))
    if not devices:
        loader.errors.append(f"{name}: no valid device entries found")
    return tuple(devices)


def load_settings(environ: Mapping[str, str]) -> Settings:
    """Validate every environment variable and build :class:`Settings`.

    Collects every failing variable before raising, so a misconfigured
    deployment sees every problem at once rather than one per restart.
    """
    loader = _Loader(environ)

    access_key = loader.require_secret("ECOFLOW_ACCESS_KEY")
    secret_key = loader.require_secret("ECOFLOW_SECRET_KEY")
    devices = _parse_devices(loader, "ECOFLOW_DEVICES")

    api_host = loader.ecoflow_api_host("ECOFLOW_API_HOST", "https://api.ecoflow.com")
    data_dir = loader.data_dir("ECOFLOW_STATS_DATA_DIR", "/data")
    tz = loader.timezone("ECOFLOW_STATS_TZ", environ.get("TZ") or "UTC")
    poll_interval = loader.int_in_range("ECOFLOW_STATS_POLL_INTERVAL", 60, 30, 3600)
    poll_offset = loader.int_in_range("ECOFLOW_STATS_POLL_OFFSET", 30, 0, 59)
    outage_threshold_v = loader.float_in_range("ECOFLOW_STATS_OUTAGE_THRESHOLD_V", 50.0, 1, 200)
    gap_threshold = loader.int_in_range("ECOFLOW_STATS_GAP_THRESHOLD", 150, 90, 3600)
    stale_threshold = loader.int_in_range(
        "ECOFLOW_STATS_STALE_THRESHOLD", poll_interval * 3, poll_interval, 86_400
    )
    tariff = loader.optional_nonneg_decimal("ECOFLOW_STATS_TARIFF")
    currency = loader.string("ECOFLOW_STATS_CURRENCY", "")
    if len(currency) > 8:
        loader.errors.append("ECOFLOW_STATS_CURRENCY: must be at most 8 characters")
    ntfy_topic = loader.optional_secret("ECOFLOW_STATS_NTFY_TOPIC")
    ntfy_url = loader.http_url("ECOFLOW_STATS_NTFY_URL", "https://ntfy.sh", ("http", "https"))
    ntfy_token = loader.optional_secret("ECOFLOW_STATS_NTFY_TOKEN")
    notify_lang = loader.enum("ECOFLOW_STATS_NOTIFY_LANG", "en", _SUPPORTED_LANGS)
    default_lang = loader.enum("ECOFLOW_STATS_DEFAULT_LANG", "en", _SUPPORTED_LANGS)
    password = loader.optional_secret("ECOFLOW_STATS_PASSWORD")
    if password is not None and len(password) < 8:
        loader.errors.append("ECOFLOW_STATS_PASSWORD: must be at least 8 characters")
    allowed_networks = loader.cidr_list("ECOFLOW_STATS_ALLOWED_NETWORKS", _DEFAULT_ALLOWED_NETWORKS)
    trusted_proxies = loader.cidr_list("ECOFLOW_STATS_TRUSTED_PROXIES", "")
    session_days = loader.int_in_range("ECOFLOW_STATS_SESSION_DAYS", 30, 1, 365)
    host = loader.string("ECOFLOW_STATS_HOST", "0.0.0.0")
    port = loader.int_in_range("ECOFLOW_STATS_PORT", 8080, 1, 65535)
    log_level = loader.enum("ECOFLOW_STATS_LOG_LEVEL", "INFO", _LOG_LEVELS)

    if loader.errors:
        raise ConfigError(loader.errors)

    assert access_key is not None  # guaranteed by the empty-errors check above
    assert secret_key is not None

    return Settings(
        access_key=access_key,
        secret_key=secret_key,
        devices=devices,
        api_host=api_host,
        data_dir=data_dir,
        tz=tz,
        poll_interval=poll_interval,
        poll_offset=poll_offset,
        outage_threshold_v=outage_threshold_v,
        gap_threshold=gap_threshold,
        stale_threshold=stale_threshold,
        tariff=tariff,
        currency=currency,
        ntfy_topic=ntfy_topic,
        ntfy_url=ntfy_url,
        ntfy_token=ntfy_token,
        notify_lang=notify_lang,
        default_lang=default_lang,
        password=password,
        allowed_networks=allowed_networks,
        trusted_proxies=trusted_proxies,
        session_days=session_days,
        host=host,
        port=port,
        log_level=log_level,
    )


__all__ = ["ConfigError", "DeviceConfig", "PublicSettings", "Settings", "load_settings"]
