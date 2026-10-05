"""Unit tests for ecoflow_stats.config.load_settings.

These are the application's fail-fast gate: every scenario here proves a
specific bad or missing environment variable is rejected, named, and never
silently defaulted or coerced.
"""

from __future__ import annotations

import dataclasses

import pytest

from ecoflow_stats.config import ConfigError, load_settings

VALID_ENV = {
    "ECOFLOW_ACCESS_KEY": "test-access-key",
    "ECOFLOW_SECRET_KEY": "test-secret-key",
    "ECOFLOW_DEVICES": "TESTDEV0001",
}


def test_missing_required_variable_names_it_in_the_error() -> None:
    env = dict(VALID_ENV)
    del env["ECOFLOW_SECRET_KEY"]

    with pytest.raises(ConfigError) as excinfo:
        load_settings(env)

    assert "ECOFLOW_SECRET_KEY" in str(excinfo.value)


def test_missing_multiple_required_variables_names_all_of_them() -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_settings({})

    message = str(excinfo.value)
    assert "ECOFLOW_ACCESS_KEY" in message
    assert "ECOFLOW_SECRET_KEY" in message
    assert "ECOFLOW_DEVICES" in message


def test_valid_configuration_returns_settings_with_documented_defaults() -> None:
    settings = load_settings(VALID_ENV)

    assert settings.access_key == "test-access-key"
    assert settings.secret_key == "test-secret-key"
    assert settings.devices[0].serial == "TESTDEV0001"
    assert settings.devices[0].adapter_id is None
    assert settings.api_host == "https://api.ecoflow.com"
    assert settings.poll_interval == 60
    assert settings.poll_offset == 30


def test_devices_entry_may_name_an_explicit_adapter_id() -> None:
    settings = load_settings(dict(VALID_ENV, ECOFLOW_DEVICES="TESTDEV0001:delta_pro"))

    assert settings.devices[0].serial == "TESTDEV0001"
    assert settings.devices[0].adapter_id == "delta_pro"


@pytest.mark.parametrize(
    ("overrides", "bad_variable"),
    [
        ({"ECOFLOW_STATS_POLL_OFFSET": "99"}, "ECOFLOW_STATS_POLL_OFFSET"),
        ({"ECOFLOW_STATS_POLL_INTERVAL": "3601"}, "ECOFLOW_STATS_POLL_INTERVAL"),
        ({"ECOFLOW_STATS_TZ": "Not/AZone"}, "ECOFLOW_STATS_TZ"),
        ({"ECOFLOW_DEVICES": "not-a-serial!!"}, "ECOFLOW_DEVICES"),
        ({"ECOFLOW_STATS_NOTIFY_LANG": "fr"}, "ECOFLOW_STATS_NOTIFY_LANG"),
        ({"ECOFLOW_STATS_DEFAULT_LANG": "fr"}, "ECOFLOW_STATS_DEFAULT_LANG"),
        ({"ECOFLOW_API_HOST": "https://evil.example.com"}, "ECOFLOW_API_HOST"),
        ({"ECOFLOW_STATS_TARIFF": "-1"}, "ECOFLOW_STATS_TARIFF"),
        ({"ECOFLOW_STATS_ALLOWED_NETWORKS": "not-a-cidr"}, "ECOFLOW_STATS_ALLOWED_NETWORKS"),
        ({"ECOFLOW_STATS_PASSWORD": "short"}, "ECOFLOW_STATS_PASSWORD"),
        ({"ECOFLOW_STATS_SESSION_DAYS": "0"}, "ECOFLOW_STATS_SESSION_DAYS"),
        ({"ECOFLOW_STATS_LOG_LEVEL": "VERBOSE"}, "ECOFLOW_STATS_LOG_LEVEL"),
        ({"ECOFLOW_STATS_NTFY_URL": "ftp://ntfy.sh"}, "ECOFLOW_STATS_NTFY_URL"),
    ],
)
def test_an_invalid_value_is_rejected_and_named(
    overrides: dict[str, str], bad_variable: str
) -> None:
    env = dict(VALID_ENV, **overrides)

    with pytest.raises(ConfigError) as excinfo:
        load_settings(env)

    assert bad_variable in str(excinfo.value)


def test_data_dir_accepts_a_path_that_does_not_yet_exist(tmp_path) -> None:
    missing = tmp_path / "does-not-exist-yet"

    settings = load_settings(dict(VALID_ENV, ECOFLOW_STATS_DATA_DIR=str(missing)))

    assert settings.data_dir == missing
    assert not missing.exists()


def test_data_dir_rejects_a_path_that_is_a_regular_file(tmp_path) -> None:
    a_file = tmp_path / "not-a-directory"
    a_file.write_text("x")

    with pytest.raises(ConfigError) as excinfo:
        load_settings(dict(VALID_ENV, ECOFLOW_STATS_DATA_DIR=str(a_file)))

    assert "ECOFLOW_STATS_DATA_DIR" in str(excinfo.value)


def test_stale_threshold_defaults_to_three_times_the_poll_interval() -> None:
    settings = load_settings(dict(VALID_ENV, ECOFLOW_STATS_POLL_INTERVAL="90"))

    assert settings.stale_threshold == 270


def test_stale_threshold_accepts_an_explicit_override() -> None:
    settings = load_settings(dict(VALID_ENV, ECOFLOW_STATS_STALE_THRESHOLD="500"))

    assert settings.stale_threshold == 500


def test_stale_threshold_below_poll_interval_is_rejected() -> None:
    env = dict(VALID_ENV, ECOFLOW_STATS_POLL_INTERVAL="60", ECOFLOW_STATS_STALE_THRESHOLD="30")

    with pytest.raises(ConfigError) as excinfo:
        load_settings(env)

    assert "ECOFLOW_STATS_STALE_THRESHOLD" in str(excinfo.value)


def test_secret_variable_accepts_a_file_fallback(tmp_path) -> None:
    secret_file = tmp_path / "access_key"
    secret_file.write_text("from-file-key\n")
    env = dict(VALID_ENV)
    del env["ECOFLOW_ACCESS_KEY"]
    env["ECOFLOW_ACCESS_KEY_FILE"] = str(secret_file)

    settings = load_settings(env)

    assert settings.access_key == "from-file-key"


def test_secret_env_var_takes_precedence_over_its_file_fallback(tmp_path) -> None:
    secret_file = tmp_path / "access_key"
    secret_file.write_text("from-file-key\n")
    env = dict(VALID_ENV, ECOFLOW_ACCESS_KEY_FILE=str(secret_file))

    settings = load_settings(env)

    assert settings.access_key == "test-access-key"


@pytest.mark.parametrize(
    ("env_name", "attribute"),
    [
        ("ECOFLOW_SECRET_KEY", "secret_key"),
        ("ECOFLOW_STATS_PASSWORD", "password"),
    ],
)
def test_the_other_two_secret_variables_also_accept_a_file_fallback(
    tmp_path, env_name: str, attribute: str
) -> None:
    """Triangulates test_secret_variable_accepts_a_file_fallback: that test
    alone would not catch a regression that special-cases only
    ECOFLOW_ACCESS_KEY_FILE and drops the other two Docker-secret variables
    task 1.5 names explicitly."""
    secret_file = tmp_path / "secret"
    secret_file.write_text("from-file-value\n")
    env = dict(VALID_ENV)
    env.pop(env_name, None)
    env[f"{env_name}_FILE"] = str(secret_file)

    settings = load_settings(env)

    assert getattr(settings, attribute) == "from-file-value"


def test_public_settings_omits_every_secret() -> None:
    settings = load_settings(
        dict(
            VALID_ENV,
            ECOFLOW_STATS_PASSWORD="a-real-password",
            ECOFLOW_STATS_NTFY_TOPIC="a-real-topic",
        )
    )

    public_values = dataclasses.asdict(settings.to_public()).values()

    assert "test-access-key" not in public_values
    assert "test-secret-key" not in public_values
    assert "a-real-password" not in public_values
    assert "a-real-topic" not in public_values
