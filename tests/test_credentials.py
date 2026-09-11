"""JSON credentials file: parsing, precedence, and secret hygiene."""

from __future__ import annotations

import json

import pytest

from worldquant.config import (
    DEFAULT_CREDENTIALS_FILENAME,
    Credentials,
    _warn_if_loosely_protected,
    credentials_from_file,
    load_config,
    require_credentials,
    resolve_credentials,
)
from worldquant.exceptions import ConfigError

ENV_USER = "WQBRAIN_USERNAME"
ENV_PASS = "WQBRAIN_PASSWORD"
ENV_CREDS_FILE = "WQBRAIN_CREDENTIALS_FILE"

SECRET = "p@ss w0rd-with spaces"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (ENV_USER, ENV_PASS, ENV_CREDS_FILE):
        monkeypatch.delenv(name, raising=False)


def write_creds(tmp_path, payload, name="credentials.json", *, raw=None):
    path = tmp_path / name
    path.write_text(raw if raw is not None else json.dumps(payload), encoding="utf-8")
    return path


class TestCredentialsFromFile:
    def test_reads_email_and_password(self, tmp_path):
        path = write_creds(tmp_path, {"email": "me@example.com", "password": SECRET})
        creds = credentials_from_file(path)
        assert creds == Credentials(username="me@example.com", password=SECRET)

    def test_username_is_accepted_as_an_alias(self, tmp_path):
        path = write_creds(tmp_path, {"username": "me@example.com", "password": SECRET})
        assert credentials_from_file(path).username == "me@example.com"

    def test_email_wins_over_username_when_both_present(self, tmp_path):
        path = write_creds(
            tmp_path, {"email": "a@example.com", "username": "b@example.com", "password": SECRET}
        )
        assert credentials_from_file(path).username == "a@example.com"

    def test_keys_are_case_insensitive(self, tmp_path):
        path = write_creds(tmp_path, {"Email": "me@example.com", "PASSWORD": SECRET})
        assert credentials_from_file(path).password == SECRET

    def test_extra_keys_are_ignored(self, tmp_path):
        # The shipped template carries a "_help" note; it must not break loading.
        path = write_creds(
            tmp_path,
            {"_help": "fill me in", "email": "me@example.com", "password": SECRET, "note": 42},
        )
        assert credentials_from_file(path).password == SECRET

    def test_password_whitespace_is_preserved(self, tmp_path):
        # Spaces can be significant in a password; only the email is stripped.
        path = write_creds(tmp_path, {"email": "  me@example.com  ", "password": "  pw  "})
        creds = credentials_from_file(path)
        assert creds.username == "me@example.com"
        assert creds.password == "  pw  "

    def test_utf8_bom_is_tolerated(self, tmp_path):
        path = tmp_path / "credentials.json"
        path.write_bytes(
            "\ufeff".encode("utf-8") + json.dumps({"email": "me@example.com", "password": SECRET}).encode("utf-8")
        )
        assert credentials_from_file(path).password == SECRET

    def test_unicode_password_survives(self, tmp_path):
        path = write_creds(tmp_path, {"email": "me@example.com", "password": "密码-pass"})
        assert credentials_from_file(path).password == "密码-pass"


class TestCredentialsFileErrors:
    def test_missing_file_raises_when_explicitly_requested(self, tmp_path):
        with pytest.raises(ConfigError) as excinfo:
            credentials_from_file(tmp_path / "nope.json", strict=True)
        assert "not found" in str(excinfo.value)

    def test_missing_file_is_none_when_auto_discovered(self, tmp_path):
        assert credentials_from_file(tmp_path / "nope.json", strict=False) is None

    @pytest.mark.parametrize("field", ["email", "password"])
    def test_placeholder_values_raise_when_explicit(self, tmp_path, field):
        payload = {"email": "me@example.com", "password": SECRET}
        payload[field] = "changeme"
        path = write_creds(tmp_path, payload)
        with pytest.raises(ConfigError) as excinfo:
            credentials_from_file(path, strict=True)
        assert "placeholder" in str(excinfo.value)

    def test_unfilled_template_falls_through_when_auto_discovered(self, tmp_path):
        path = write_creds(tmp_path, {"email": "you@example.com", "password": "your-password"})
        assert credentials_from_file(path, strict=False) is None

    def test_malformed_json_raises_even_when_auto_discovered(self, tmp_path):
        path = write_creds(tmp_path, None, raw='{"email": "me@example.com", "password": ')
        with pytest.raises(ConfigError) as excinfo:
            credentials_from_file(path, strict=False)
        assert "not valid JSON" in str(excinfo.value)

    def test_malformed_json_error_does_not_leak_the_password(self, tmp_path):
        path = write_creds(
            tmp_path, None, raw=f'{{"email": "me@example.com", "password": "{SECRET}"'
        )
        with pytest.raises(ConfigError) as excinfo:
            credentials_from_file(path)
        assert SECRET not in str(excinfo.value)

    def test_non_object_json_raises(self, tmp_path):
        path = write_creds(tmp_path, None, raw='["me@example.com", "pw"]')
        with pytest.raises(ConfigError) as excinfo:
            credentials_from_file(path)
        assert "JSON object" in str(excinfo.value)

    def test_missing_password_raises_with_the_expected_shape(self, tmp_path):
        path = write_creds(tmp_path, {"email": "me@example.com"})
        with pytest.raises(ConfigError) as excinfo:
            credentials_from_file(path)
        assert "password" in str(excinfo.value)
        assert '"email"' in str(excinfo.value)

    def test_non_string_password_raises(self, tmp_path):
        path = write_creds(tmp_path, {"email": "me@example.com", "password": 12345})
        with pytest.raises(ConfigError):
            credentials_from_file(path)

    def test_misspelled_key_is_reported_as_missing(self, tmp_path):
        path = write_creds(tmp_path, {"email": "me@example.com", "passwrod": SECRET})
        with pytest.raises(ConfigError) as excinfo:
            credentials_from_file(path)
        assert "password" in str(excinfo.value)


class TestPrecedence:
    def test_explicit_path_beats_environment_variables(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV_USER, "env@example.com")
        monkeypatch.setenv(ENV_PASS, "env-password")
        path = write_creds(tmp_path, {"email": "file@example.com", "password": SECRET}, name="explicit.json")

        creds = resolve_credentials(tmp_path, explicit_path=path)
        assert creds.username == "file@example.com"

    def test_env_file_variable_beats_inline_credentials(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV_USER, "env@example.com")
        monkeypatch.setenv(ENV_PASS, "env-password")
        path = write_creds(tmp_path, {"email": "named@example.com", "password": SECRET}, name="named.json")
        monkeypatch.setenv(ENV_CREDS_FILE, str(path))

        assert resolve_credentials(tmp_path).username == "named@example.com"

    def test_inline_env_beats_the_auto_discovered_file(self, tmp_path, monkeypatch):
        write_creds(tmp_path, {"email": "auto@example.com", "password": SECRET})
        monkeypatch.setenv(ENV_USER, "env@example.com")
        monkeypatch.setenv(ENV_PASS, "env-password")

        assert resolve_credentials(tmp_path).username == "env@example.com"

    def test_auto_discovered_file_is_used_when_nothing_else_is_set(self, tmp_path):
        write_creds(tmp_path, {"email": "auto@example.com", "password": SECRET})
        creds = resolve_credentials(tmp_path)
        assert creds == Credentials(username="auto@example.com", password=SECRET)

    def test_auto_discovered_template_falls_through_to_none(self, tmp_path):
        write_creds(tmp_path, {"email": "you@example.com", "password": "your-password"})
        assert resolve_credentials(tmp_path) is None

    def test_relative_env_file_path_resolves_against_the_root(self, tmp_path, monkeypatch):
        write_creds(tmp_path, {"email": "rel@example.com", "password": SECRET}, name="rel.json")
        monkeypatch.setenv(ENV_CREDS_FILE, "rel.json")
        assert resolve_credentials(tmp_path).username == "rel@example.com"

    def test_env_file_pointing_at_a_missing_path_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV_CREDS_FILE, str(tmp_path / "gone.json"))
        with pytest.raises(ConfigError) as excinfo:
            resolve_credentials(tmp_path)
        assert "not found" in str(excinfo.value)

    def test_no_sources_at_all_is_none(self, tmp_path):
        assert resolve_credentials(tmp_path) is None


class TestLoadConfigIntegration:
    def test_load_config_picks_up_the_credentials_file(self, tmp_path):
        write_creds(tmp_path, {"email": "me@example.com", "password": SECRET})
        assert load_config(None, root=tmp_path).credentials.username == "me@example.com"

    def test_load_config_credentials_path_argument_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV_USER, "env@example.com")
        monkeypatch.setenv(ENV_PASS, "env-password")
        write_creds(tmp_path, {"email": "auto@example.com", "password": "auto"})
        explicit = write_creds(
            tmp_path, {"email": "explicit@example.com", "password": SECRET}, name="explicit.json"
        )

        config = load_config(None, root=tmp_path, credentials_path=explicit)
        assert config.credentials.username == "explicit@example.com"

    def test_broken_explicit_credentials_file_is_a_config_error(self, tmp_path):
        bad = write_creds(tmp_path, None, name="bad.json", raw="{oops")
        with pytest.raises(ConfigError):
            load_config(None, root=tmp_path, credentials_path=bad)

    def test_require_credentials_mentions_every_source(self, tmp_path):
        config = load_config(None, root=tmp_path)
        with pytest.raises(ConfigError) as excinfo:
            require_credentials(config)
        message = str(excinfo.value)
        assert DEFAULT_CREDENTIALS_FILENAME in message
        assert ENV_CREDS_FILE in message
        assert ENV_USER in message


class TestSecretHygiene:
    def test_password_is_not_in_the_repr(self):
        assert SECRET not in repr(Credentials("me@example.com", SECRET))

    def test_password_is_not_logged(self, tmp_path, caplog):
        path = write_creds(tmp_path, {"email": "me@example.com", "password": SECRET})
        with caplog.at_level("DEBUG", logger="worldquant"):
            credentials_from_file(path)
        assert SECRET not in caplog.text
        # The username is not secret and is useful for confirming which account loaded.
        assert "me@example.com" in caplog.text

    def test_placeholder_skip_is_not_logged_with_content(self, tmp_path, caplog):
        path = write_creds(tmp_path, {"email": "you@example.com", "password": "your-password"})
        with caplog.at_level("DEBUG", logger="worldquant"):
            credentials_from_file(path, strict=False)
        assert "your-password" not in caplog.text


class _StubStat:
    def __init__(self, mode: int) -> None:
        self.st_mode = mode


class _StubPath:
    """Stands in for ``Path`` so permission checks are platform-independent.

    ``os.chmod`` on Windows only toggles the read-only bit, so real files there
    always report 0o666 and cannot exercise the tight-permission branch.
    """

    def __init__(self, mode: int, name: str = "credentials.json") -> None:
        self._mode = mode
        self._name = name

    def stat(self) -> _StubStat:
        return _StubStat(self._mode)

    def __str__(self) -> str:
        return self._name


class TestPermissionWarning:
    @pytest.mark.parametrize("mode", [0o644, 0o664, 0o666, 0o777, 0o604])
    def test_posix_group_or_other_readable_warns(self, caplog, mode):
        with caplog.at_level("WARNING", logger="worldquant"):
            _warn_if_loosely_protected(_StubPath(mode), posix=True)
        assert "chmod 600" in caplog.text
        assert f"{mode:o}" in caplog.text

    @pytest.mark.parametrize("mode", [0o600, 0o400])
    def test_posix_owner_only_file_does_not_warn(self, caplog, mode):
        with caplog.at_level("WARNING", logger="worldquant"):
            _warn_if_loosely_protected(_StubPath(mode), posix=True)
        assert caplog.text == ""

    def test_non_posix_warns_about_plaintext_storage(self, caplog):
        with caplog.at_level("WARNING", logger="worldquant"):
            _warn_if_loosely_protected(_StubPath(0o600), posix=False)
        assert "plaintext" in caplog.text
        # On Windows st_mode cannot express ACLs, so chmod advice would be noise.
        assert "chmod" not in caplog.text

    def test_platform_is_detected_when_not_overridden(self, caplog):
        with caplog.at_level("WARNING", logger="worldquant"):
            _warn_if_loosely_protected(_StubPath(0o666))
        assert caplog.text, "every platform should warn about a 0o666 credentials file"

    def test_warning_never_contains_the_password(self, tmp_path, caplog):
        path = write_creds(tmp_path, {"email": "me@example.com", "password": SECRET})
        with caplog.at_level("WARNING", logger="worldquant"):
            credentials_from_file(path)
        assert SECRET not in caplog.text


class TestShippedFiles:
    def test_example_template_is_present_and_ignored_as_a_placeholder(self):
        from worldquant.config import PROJECT_ROOT

        example = PROJECT_ROOT / "credentials.example.json"
        assert example.exists(), "credentials.example.json must ship with the project"
        # Copying the template verbatim must not silently log in as "you@example.com".
        assert credentials_from_file(example, strict=False) is None
        with pytest.raises(ConfigError):
            credentials_from_file(example, strict=True)

    def test_example_template_is_gitignored_safe(self):
        from worldquant.config import PROJECT_ROOT

        ignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "credentials.json" in ignore
        assert "!credentials.example.json" in ignore
