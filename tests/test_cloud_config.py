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
    legacy = {"_env_file": dotenv, "pa_cloud_provider": "deepseek"}
    assert not Settings(**legacy).has_cloud
    monkeypatch.setenv("DEEPSEEK_API_KEY", "explicit-env-key")
    assert Settings(**legacy).cloud_profile.api_key == "explicit-env-key"
    assert not Settings(**legacy, deepseek_base_url="https://other.test/v1").has_cloud
    assert Settings(**legacy, deepseek_base_url="https://other.test/v1", deepseek_api_key="explicit").has_cloud


def test_terra_defaults_do_not_use_the_legacy_key(tmp_path, monkeypatch):
    for prefix in ("TERRA", "DEEPSEEK"):
        for name in ("API_KEY", "BASE_URL", "MODEL", "EFFORT"):
            monkeypatch.delenv(f"{prefix}_{name}", raising=False)
    monkeypatch.delenv("PA_CLOUD_PROVIDER", raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text("DEEPSEEK_API_KEY=legacy-key\n", encoding="utf-8")
    prefs = Settings(_env_file=dotenv)
    cloud = prefs.cloud_profile
    assert (cloud.provider, cloud.base_url, cloud.model, cloud.effort) == (
        "terra", "https://api.muzeai.top/v1", "gpt-6.1-terra", "medium"
    )
    assert not prefs.has_cloud
    assert Settings(_env_file=dotenv, pa_cloud_provider="deepseek").cloud_profile.api_key == "legacy-key"


def test_terra_model_and_effort_are_selectable_and_key_stays_with_endpoint(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text("TERRA_BASE_URL=https://terra.test/v1\nTERRA_API_KEY=terra-key\n"
                      "TERRA_MODEL=alternate-model\nTERRA_EFFORT=low\n", encoding="utf-8")
    config = {"_env_file": dotenv, "pa_cloud_provider": "terra"}
    monkeypatch.delenv("TERRA_BASE_URL", raising=False)
    monkeypatch.setenv("TERRA_API_KEY", "unrelated-key")
    chosen = Settings(**config).cloud_profile
    assert (chosen.model, chosen.effort, chosen.api_key) == ("alternate-model", "low", "terra-key")
    monkeypatch.setenv("TERRA_BASE_URL", "https://new.test/v1")
    monkeypatch.delenv("TERRA_API_KEY")
    assert not Settings(**config).has_cloud
    assert not Settings(**config, terra_base_url="https://third.test/v1").has_cloud
