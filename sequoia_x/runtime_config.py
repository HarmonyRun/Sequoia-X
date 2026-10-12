"""Persistent settings used by the fnOS dashboard and worker processes."""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DATA_SOURCES = {"auto", "eastmoney", "tencent", "sina", "baostock"}
_LOCK = threading.RLock()
_SECRET_FIELDS = {"feishu_webhook_url", "llm_api_key", "dashboard_password"}


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _defaults() -> dict[str, Any]:
    return {
        "data_source": os.getenv("DATA_SOURCE", "auto"),
        "start_date": os.getenv("START_DATE", "2024-01-01"),
        "run_time": os.getenv("RUN_TIME", "16:00"),
        "run_weekdays": os.getenv("RUN_WEEKDAYS", "1,2,3,4,5"),
        "auto_backfill": _as_bool(os.getenv("AUTO_BACKFILL"), True),
        "feishu_webhook_url": os.getenv("FEISHU_WEBHOOK_URL", ""),
        "push_strategy_results": _as_bool(os.getenv("PUSH_STRATEGY_RESULTS"), True),
        "push_ai_analysis": _as_bool(os.getenv("PUSH_AI_ANALYSIS"), True),
        "llm_enabled": _as_bool(os.getenv("LLM_ENABLED"), False),
        "llm_base_url": os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
        "llm_api_key": os.getenv("LLM_API_KEY", ""),
        "llm_model": os.getenv("LLM_MODEL", ""),
        "llm_temperature": float(os.getenv("LLM_TEMPERATURE", "0.2")),
        "llm_extra_prompt": os.getenv("LLM_EXTRA_PROMPT", ""),
        "dashboard_username": os.getenv("DASHBOARD_USERNAME", "admin"),
        "dashboard_password": os.getenv("DASHBOARD_PASSWORD", ""),
    }


class RuntimeConfigStore:
    """Small JSON store. Secrets are only exposed to worker environment variables."""

    def __init__(self, path: str | Path | None = None) -> None:
        data_dir = Path(os.getenv("SEQUOIA_DATA_DIR", "data"))
        self.path = Path(path or os.getenv("SEQUOIA_CONFIG_FILE", data_dir / "settings.json"))

    def load(self) -> dict[str, Any]:
        result = _defaults()
        with _LOCK:
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    result.update({key: value for key, value in saved.items() if key in result})
            except (OSError, ValueError):
                pass
        return result

    @staticmethod
    def _validate(values: dict[str, Any]) -> dict[str, Any]:
        allowed = set(_defaults())
        clean = {key: value for key, value in values.items() if key in allowed}
        if "data_source" in clean and clean["data_source"] not in DATA_SOURCES:
            raise ValueError("不支持的数据源")
        if "start_date" in clean and not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", str(clean["start_date"])
        ):
            raise ValueError("起始日期格式应为 YYYY-MM-DD")
        if "run_time" in clean:
            match = re.fullmatch(r"(\d{2}):(\d{2})", str(clean["run_time"]))
            if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
                raise ValueError("自动运行时间格式应为 HH:MM")
        if "run_weekdays" in clean:
            try:
                weekdays = {int(item.strip()) for item in str(clean["run_weekdays"]).split(",")}
            except ValueError as exc:
                raise ValueError("运行星期应使用 1 到 7 的逗号分隔数字") from exc
            if not weekdays or not weekdays <= set(range(1, 8)):
                raise ValueError("运行星期应使用 1 到 7 的逗号分隔数字")
            clean["run_weekdays"] = ",".join(str(item) for item in sorted(weekdays))
        if "dashboard_username" in clean and not str(clean["dashboard_username"]).strip():
            raise ValueError("管理页用户名不能为空")
        for key in ("llm_base_url", "feishu_webhook_url"):
            value = str(clean.get(key, "")).strip()
            if value and urlparse(value).scheme not in {"http", "https"}:
                raise ValueError(f"{key} 必须是 HTTP(S) 地址")
            if key in clean:
                clean[key] = value
        if "llm_temperature" in clean:
            temperature = float(clean["llm_temperature"])
            if not 0 <= temperature <= 2:
                raise ValueError("temperature 必须在 0 到 2 之间")
            clean["llm_temperature"] = temperature
        for key in ("auto_backfill", "push_strategy_results", "push_ai_analysis", "llm_enabled"):
            if key in clean:
                clean[key] = _as_bool(clean[key])
        return clean

    def save(self, updates: dict[str, Any]) -> dict[str, Any]:
        clean = self._validate(updates)
        with _LOCK:
            current = self.load()
            for secret in _SECRET_FIELDS:
                # Blank secret fields mean "keep the existing value" in the dashboard.
                if secret in clean and not clean[secret]:
                    clean.pop(secret)
            current.update(clean)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
            os.chmod(temp, 0o600)
            temp.replace(self.path)
        return current

    def public(self) -> dict[str, Any]:
        values = self.load()
        for secret in _SECRET_FIELDS:
            values[f"{secret}_configured"] = bool(values.get(secret))
            values[secret] = ""
        return values

    def worker_env(self) -> dict[str, str]:
        values = self.load()
        mapping = {
            "data_source": "DATA_SOURCE",
            "start_date": "START_DATE",
            "feishu_webhook_url": "FEISHU_WEBHOOK_URL",
            "push_strategy_results": "PUSH_STRATEGY_RESULTS",
            "push_ai_analysis": "PUSH_AI_ANALYSIS",
            "llm_enabled": "LLM_ENABLED",
            "llm_base_url": "LLM_BASE_URL",
            "llm_api_key": "LLM_API_KEY",
            "llm_model": "LLM_MODEL",
            "llm_temperature": "LLM_TEMPERATURE",
            "llm_extra_prompt": "LLM_EXTRA_PROMPT",
        }
        return {
            env_name: ("true" if value is True else "false" if value is False else str(value))
            for key, env_name in mapping.items()
            if (value := values.get(key)) is not None
        }
