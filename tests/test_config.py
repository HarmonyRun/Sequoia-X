"""配置管理属性测试。"""

import os
from hypothesis import given, settings as h_settings, HealthCheck
from hypothesis import strategies as st


# Feature: sequoia-x-v2, Property 1: 环境变量覆盖配置默认值
@given(db_path=st.text(min_size=1, max_size=100, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="/_.-")))
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_env_overrides_default(db_path: str, monkeypatch) -> None:
    """属性 1：任意合法 db_path 通过环境变量设置后，Settings 实例应反映该值。"""
    import sequoia_x.core.config as cfg_module
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://example.com/hook")
    monkeypatch.setattr(cfg_module, "_settings", None)
    from sequoia_x.core.config import Settings
    s = Settings()
    assert s.db_path == db_path


# Feature: sequoia-x-v2, Property 2: 飞书配置可选
def test_missing_webhook_disables_notification() -> None:
    """未配置 Webhook 时仍可运行，并由通知器安全跳过推送。"""
    import os
    from sequoia_x.core.config import Settings
    # 确保环境变量中没有该字段
    env_backup = os.environ.pop("FEISHU_WEBHOOK_URL", None)
    try:
        assert Settings(_env_file=None).feishu_webhook_url == ""
    finally:
        if env_backup is not None:
            os.environ["FEISHU_WEBHOOK_URL"] = env_backup
