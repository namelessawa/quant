"""Configuration loading, precedence and validation."""

from __future__ import annotations

import json

import pytest

from worldquant.api import DEFAULT_SETTINGS
from worldquant.config import (
    MAX_CONCURRENCY,
    Credentials,
    FilterConfig,
    apply_overrides,
    credentials_from_env,
    load_config,
    require_credentials,
    validate_filter_config,
)
from worldquant.exceptions import ConfigError

ENV_USER = "WQBRAIN_USERNAME"
ENV_PASS = "WQBRAIN_PASSWORD"
ENV_CREDS_FILE = "WQBRAIN_CREDENTIALS_FILE"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (ENV_USER, ENV_PASS, ENV_CREDS_FILE, "WQBRAIN_BASE_URL",
                 "WQBRAIN_DB_PATH", "WQBRAIN_LOG_LEVEL", "WQBRAIN_YEARLY_PATH"):
        monkeypatch.delenv(name, raising=False)


def write_yaml(tmp_path, text, name="config.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


class TestCredentials:
    def test_read_from_the_environment(self, monkeypatch):
        monkeypatch.setenv(ENV_USER, "me@example.com")
        monkeypatch.setenv(ENV_PASS, "pw")
        creds = credentials_from_env()
        assert creds == Credentials(username="me@example.com", password="pw")

    def test_absent_when_neither_is_set(self):
        assert credentials_from_env() is None

    def test_half_configured_raises(self, monkeypatch):
        monkeypatch.setenv(ENV_USER, "me@example.com")
        with pytest.raises(ConfigError) as excinfo:
            credentials_from_env()
        assert ENV_PASS in str(excinfo.value)

    def test_username_is_stripped(self, monkeypatch):
        monkeypatch.setenv(ENV_USER, "  me@example.com  ")
        monkeypatch.setenv(ENV_PASS, "pw")
        assert credentials_from_env().username == "me@example.com"

    def test_password_is_not_stripped(self, monkeypatch):
        # Leading/trailing spaces can be significant in a password.
        monkeypatch.setenv(ENV_USER, "me@example.com")
        monkeypatch.setenv(ENV_PASS, " pw ")
        assert credentials_from_env().password == " pw "

    def test_repr_hides_the_password(self):
        assert "pw" not in repr(Credentials("me@example.com", "pw"))

    def test_require_credentials_raises_when_missing(self, tmp_path):
        config = load_config(None, root=tmp_path)
        with pytest.raises(ConfigError) as excinfo:
            require_credentials(config)
        assert ENV_USER in str(excinfo.value)

    def test_require_credentials_returns_them_when_present(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ENV_USER, "me@example.com")
        monkeypatch.setenv(ENV_PASS, "pw")
        config = load_config(None, root=tmp_path)
        assert require_credentials(config).username == "me@example.com"


class TestFileLoading:
    def test_defaults_apply_with_no_file(self, tmp_path):
        config = load_config(None, root=tmp_path)
        assert config.settings == DEFAULT_SETTINGS
        assert config.filters.min_sharpe == 1.25
        assert config.runner.concurrency == 2

    def test_yaml_settings_override_defaults(self, tmp_path):
        path = write_yaml(tmp_path, "settings:\n  region: CHN\n  delay: 0\n")
        config = load_config(path)
        assert config.settings["region"] == "CHN"
        assert config.settings["delay"] == 0
        # Untouched keys keep their defaults.
        assert config.settings["universe"] == "TOP3000"

    def test_json_config_is_also_accepted(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"settings": {"region": "GLB"}}), encoding="utf-8")
        assert load_config(path).settings["region"] == "GLB"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(ConfigError) as excinfo:
            load_config(tmp_path / "nope.yaml")
        assert "not found" in str(excinfo.value)

    def test_non_mapping_document_raises(self, tmp_path):
        path = write_yaml(tmp_path, "- just\n- a\n- list\n")
        with pytest.raises(ConfigError):
            load_config(path)

    def test_unknown_filter_key_raises(self, tmp_path):
        path = write_yaml(tmp_path, "filters:\n  min_sharpe_ratio: 1.0\n")
        with pytest.raises(ConfigError) as excinfo:
            load_config(path)
        assert "unknown keys" in str(excinfo.value)
        assert "min_sharpe" in str(excinfo.value)

    def test_unknown_runner_key_raises(self, tmp_path):
        path = write_yaml(tmp_path, "runner:\n  poll_intervall: 5\n")
        with pytest.raises(ConfigError):
            load_config(path)

    def test_null_setting_value_keeps_the_default(self, tmp_path):
        path = write_yaml(tmp_path, "settings:\n  region: null\n")
        assert load_config(path).settings["region"] == "USA"

    def test_filters_section_is_read(self, tmp_path):
        path = write_yaml(
            tmp_path,
            "filters:\n  min_sharpe: 2.0\n  max_turnover: 0.5\n"
            "  min_positive_year_ratio: 0.8\n  max_negative_sharpe_years: 0\n",
        )
        filters = load_config(path).filters
        assert filters.min_sharpe == 2.0
        assert filters.max_turnover == 0.5
        assert filters.min_positive_year_ratio == 0.8
        assert filters.max_negative_sharpe_years == 0

    def test_null_disables_a_filter_rule(self, tmp_path):
        path = write_yaml(tmp_path, "filters:\n  min_sharpe: null\n")
        assert load_config(path).filters.min_sharpe is None

    def test_yearly_stats_section(self, tmp_path):
        path = write_yaml(
            tmp_path, "yearly_stats:\n  enabled: false\n  path: /alphas/{alpha_id}/yearly\n"
        )
        config = load_config(path)
        assert config.fetch_yearly_stats is False
        assert config.yearly_stats_path == "/alphas/{alpha_id}/yearly"


class TestEnvironmentOverrides:
    def test_base_url_from_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("WQBRAIN_BASE_URL", "https://example.test/api/")
        assert load_config(None, root=tmp_path).base_url == "https://example.test/api"

    def test_db_path_from_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("WQBRAIN_DB_PATH", str(tmp_path / "custom.db"))
        assert load_config(None, root=tmp_path).storage.db_path == tmp_path / "custom.db"

    def test_log_level_from_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("WQBRAIN_LOG_LEVEL", "debug")
        assert load_config(None, root=tmp_path).storage.log_level == "DEBUG"

    def test_yearly_path_from_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("WQBRAIN_YEARLY_PATH", "/alphas/{alpha_id}/yearly")
        assert load_config(None, root=tmp_path).yearly_stats_path == "/alphas/{alpha_id}/yearly"

    def test_env_beats_the_config_file(self, monkeypatch, tmp_path):
        path = write_yaml(tmp_path, "base_url: https://from-file.test\n")
        monkeypatch.setenv("WQBRAIN_BASE_URL", "https://from-env.test")
        assert load_config(path).base_url == "https://from-env.test"

    def test_explicit_overrides_beat_the_environment(self, monkeypatch, tmp_path):
        monkeypatch.setenv("WQBRAIN_BASE_URL", "https://from-env.test")
        config = load_config(None, root=tmp_path, overrides={"base_url": "https://override.test"})
        assert config.base_url == "https://override.test"


class TestApplyOverrides:
    def test_runner_overrides(self, tmp_path):
        config = load_config(None, root=tmp_path)
        updated = apply_overrides(config, {"poll_interval": 20.0, "max_wait": 60.0})
        assert updated.runner.poll_interval == 20.0
        assert updated.runner.max_wait == 60.0

    def test_none_values_are_ignored(self, tmp_path):
        config = load_config(None, root=tmp_path)
        assert apply_overrides(config, {"poll_interval": None}).runner.poll_interval == \
            config.runner.poll_interval

    def test_settings_are_merged(self, tmp_path):
        config = load_config(None, root=tmp_path)
        updated = apply_overrides(config, {"settings": {"region": "CHN"}})
        assert updated.settings["region"] == "CHN"
        assert updated.settings["universe"] == "TOP3000"

    def test_unknown_override_raises(self, tmp_path):
        config = load_config(None, root=tmp_path)
        with pytest.raises(ConfigError) as excinfo:
            apply_overrides(config, {"nonsense": 1})
        assert "nonsense" in str(excinfo.value)

    def test_original_config_is_not_mutated(self, tmp_path):
        config = load_config(None, root=tmp_path)
        apply_overrides(config, {"poll_interval": 99.0})
        assert config.runner.poll_interval == 10.0


class TestConcurrencyClamping:
    def test_default_is_conservative(self, tmp_path):
        assert load_config(None, root=tmp_path).runner.concurrency == 2

    def test_above_the_ceiling_is_clamped(self, tmp_path):
        config = load_config(None, root=tmp_path, overrides={"concurrency": 50})
        assert config.runner.concurrency == MAX_CONCURRENCY

    def test_clamped_via_the_config_file_too(self, tmp_path):
        path = write_yaml(tmp_path, "runner:\n  concurrency: 25\n")
        assert load_config(path).runner.concurrency == MAX_CONCURRENCY

    def test_below_one_is_raised(self, tmp_path):
        assert load_config(None, root=tmp_path, overrides={"concurrency": 0}).runner.concurrency == 1

    def test_within_range_is_untouched(self, tmp_path):
        assert load_config(None, root=tmp_path, overrides={"concurrency": 3}).runner.concurrency == 3


class TestValidation:
    def test_default_config_is_valid(self, tmp_path):
        validate_filter_config(load_config(None, root=tmp_path).filters)

    def test_ratio_out_of_range_raises(self):
        with pytest.raises(ConfigError):
            validate_filter_config(FilterConfig(min_positive_year_ratio=1.5))

    def test_negative_ratio_raises(self):
        with pytest.raises(ConfigError):
            validate_filter_config(FilterConfig(min_positive_year_ratio=-0.1))

    def test_non_numeric_threshold_raises(self):
        with pytest.raises(ConfigError):
            validate_filter_config(FilterConfig(min_sharpe="high"))  # type: ignore[arg-type]

    def test_negative_year_count_raises(self):
        with pytest.raises(ConfigError):
            validate_filter_config(FilterConfig(max_negative_sharpe_years=-1))

    def test_percent_shaped_turnover_warns(self, caplog):
        with caplog.at_level("WARNING", logger="worldquant"):
            validate_filter_config(FilterConfig(max_turnover=70.0))
        assert "decimal fraction" in caplog.text

    def test_disabled_rules_never_warn(self, caplog):
        with caplog.at_level("WARNING", logger="worldquant"):
            validate_filter_config(
                FilterConfig(min_sharpe=None, min_fitness=None, max_turnover=None)
            )
        assert caplog.text == ""


class TestProjectConfigFile:
    def test_the_shipped_config_yaml_loads(self):
        from worldquant.config import PROJECT_ROOT

        config = load_config(PROJECT_ROOT / "config.yaml")
        assert config.settings["region"] == "USA"
        assert config.filters.min_sharpe == 1.25
        assert config.runner.concurrency == 2
        validate_filter_config(config.filters)
