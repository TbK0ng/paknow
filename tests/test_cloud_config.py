"""Credentials follow the selected cloud endpoint, not unrelated shell exports."""
from paknow.config import Settings


def test_selected_dotenv_profile_is_not_contaminated_by_key_only_environment(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text("DEEPSEEK_BASE_URL=https://www.micuapi.ai/v1\nDEEPSEEK_API_KEY=local-key\n"
                      "DEEPSEEK_MODEL=deepseek-v4.1-flash\nDEEPSEEK_EFFORT=high\n", encoding="utf-8")
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unrelated-inherited-key")
    monkeypatch.setenv("DEEPSEEK_MODEL", "unrelated-inherited-model")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:7333")
    settings = Settings(_env_file=dotenv)
    assert settings.deepseek_api_key == "local-key"
    assert settings.deepseek_model == "deepseek-v4.1-flash"
    assert settings.qdrant_url == "http://localhost:7333"


def test_explicit_provider_environment_does_not_inherit_a_different_dotenv_key(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text("DEEPSEEK_BASE_URL=https://www.micuapi.ai/v1\nDEEPSEEK_API_KEY=local-key\n", encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://example.test/v1")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert not Settings(_env_file=dotenv).has_cloud
    monkeypatch.setenv("DEEPSEEK_API_KEY", "explicit-env-key")
    assert Settings(_env_file=dotenv).deepseek_api_key == "explicit-env-key"
    assert not Settings(_env_file=dotenv, deepseek_base_url="https://other.test/v1").has_cloud
    assert Settings(_env_file=dotenv, deepseek_base_url="https://other.test/v1", deepseek_api_key="explicit").has_cloud
