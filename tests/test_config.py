import pytest

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
