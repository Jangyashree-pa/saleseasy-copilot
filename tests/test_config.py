import sys
from types import ModuleType

import pytest

from app import config
from app.config import ConfigError, load_settings


def test_defaults_to_mock_without_a_key():
    assert load_settings({}).llm_provider == "mock"


def test_uses_anthropic_when_key_present():
    s = load_settings({"ANTHROPIC_API_KEY": "sk-ant-x"})
    assert s.llm_provider == "anthropic"
    assert s.anthropic_model == "claude-haiku-5-5"


def test_explicit_provider_without_key_fails_loudly():
    with pytest.raises(ConfigError):
        load_settings({"LLM_PROVIDER": "anthropic"})
    with pytest.raises(ConfigError):
        load_settings({"LLM_PROVIDER": "openai_compatible", "OPENAI_API_KEY": "k"})  # model missing


def test_repr_never_shows_secrets():
    s = load_settings({"ANTHROPIC_API_KEY": "sk-ant-SECRET-VALUE"})
    assert "SECRET" not in repr(s)


def test_invalid_values_rejected():
    with pytest.raises(ConfigError):
        load_settings({"LLM_EFFORT": "turbo"})
    with pytest.raises(ConfigError):
        load_settings({"RATE_LIMIT_PER_MINUTE": "lots"})


def test_settings_load_from_process_environment(monkeypatch):
    monkeypatch.setattr(config, "_load_dotenv", lambda: None)
    monkeypatch.setenv("LLM_PROVIDER", "mock")

    settings = load_settings()

    assert settings.llm_provider == "mock"
    assert settings.model_name == "rules-baseline"


def test_dotenv_file_is_loaded_without_overriding_environment(monkeypatch):
    loaded = []
    dotenv = ModuleType("dotenv")
    dotenv.load_dotenv = lambda path, override: loaded.append((path.name, override))
    monkeypatch.setattr(config.Path, "exists", lambda self: True)
    monkeypatch.setitem(sys.modules, "dotenv", dotenv)

    config._load_dotenv()

    assert loaded == [(".env", False)]


def test_missing_dotenv_dependency_is_optional(monkeypatch):
    monkeypatch.setattr(config.Path, "exists", lambda self: True)
    monkeypatch.setitem(sys.modules, "dotenv", None)

    config._load_dotenv()


def test_missing_dotenv_file_is_ignored(monkeypatch):
    monkeypatch.setattr(config.Path, "exists", lambda self: False)

    config._load_dotenv()


@pytest.mark.parametrize("value,expected", [(" yes ", True), ("off", False)])
def test_boolean_setting_parses_nonempty_values(value, expected):
    settings = load_settings({"ALLOW_FAILURE_SIMULATION": value})

    assert settings.allow_failure_simulation is expected


@pytest.mark.parametrize(
    "key,value",
    [("RATE_LIMIT_PER_MINUTE", "0"), ("MAX_INPUT_CHARS", "49"), ("MAX_BODY_BYTES", "1023")],
)
def test_integer_settings_enforce_minimums(key, value):
    with pytest.raises(ConfigError, match="must be >="):
        load_settings({key: value})


def test_invalid_timeout_is_rejected():
    with pytest.raises(ConfigError, match="LLM_TIMEOUT_SECONDS must be a number"):
        load_settings({"LLM_TIMEOUT_SECONDS": "soon"})


def test_invalid_openai_json_mode_is_rejected():
    with pytest.raises(ConfigError, match="OPENAI_JSON_MODE"):
        load_settings({"OPENAI_JSON_MODE": "xml"})


def test_unknown_provider_is_rejected():
    with pytest.raises(ConfigError, match="LLM_PROVIDER must be one of"):
        load_settings({"LLM_PROVIDER": "unknown"})


def test_valid_openai_settings_and_model_name():
    settings = load_settings(
        {
            "LLM_PROVIDER": "openai_compatible",
            "OPENAI_API_KEY": "key",
            "OPENAI_MODEL": "test-model",
            "OPENAI_BASE_URL": "https://example.test/v1///",
            "OPENAI_JSON_MODE": "JSON_OBJECT",
            "LLM_EFFORT": "HIGH",
        }
    )

    assert settings.model_name == "test-model"
    assert settings.openai_base_url == "https://example.test/v1"
    assert settings.openai_json_mode == "json_object"
    assert settings.llm_effort == "high"


def test_valid_integer_setting_is_parsed():
    settings = load_settings({"MAX_INPUT_CHARS": "2500"})

    assert settings.max_input_chars == 2500
