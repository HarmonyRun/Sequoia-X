from sequoia_x.runtime_config import RuntimeConfigStore


def test_runtime_config_masks_and_preserves_secrets(tmp_path) -> None:
    store = RuntimeConfigStore(tmp_path / "settings.json")
    store.save(
        {
            "feishu_webhook_url": "https://example.com/hook",
            "llm_api_key": "secret-key",
            "dashboard_password": "secret-password",
            "data_source": "tencent",
        }
    )
    public = store.public()
    assert public["llm_api_key"] == ""
    assert public["llm_api_key_configured"] is True
    assert public["feishu_webhook_url"] == ""
    store.save({"llm_api_key": "", "feishu_webhook_url": ""})
    assert store.load()["llm_api_key"] == "secret-key"
    assert store.load()["feishu_webhook_url"] == "https://example.com/hook"


def test_runtime_config_worker_env(tmp_path) -> None:
    store = RuntimeConfigStore(tmp_path / "settings.json")
    store.save({"llm_enabled": True, "llm_model": "test-model", "data_source": "auto"})
    env = store.worker_env()
    assert env["LLM_ENABLED"] == "true"
    assert env["LLM_MODEL"] == "test-model"
    assert env["DATA_SOURCE"] == "auto"
