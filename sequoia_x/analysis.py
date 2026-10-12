"""OpenAI-compatible analysis of strategy results."""

from __future__ import annotations

from typing import Any

import requests

from sequoia_x.core.config import Settings


class LlmAnalyzer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def analyze(self, strategy_results: list[dict[str, Any]]) -> str:
        if not self.settings.llm_enabled:
            raise ValueError("大模型分析尚未启用")
        if not self.settings.llm_api_key or not self.settings.llm_model:
            raise ValueError("请先配置大模型 API Key 和模型名称")
        compact = "\n".join(
            (
                f"- {item['name']}（{item['strategy']}）\n"
                f"  策略逻辑：{item.get('description', '未提供')}\n"
                f"  命中股票：{', '.join(item['symbols']) or '无结果'}"
            )
            for item in strategy_results
        )
        prompt = (
            "请严格结合每个 A 股量化策略的逻辑和对应选股结果做二次分析。说明命中条件、"
            "多策略共振、主要风险、值得优先人工复核的标的与理由，并给出清晰的观察清单。不得承诺收益，"
            "需明确内容仅供研究参考。\n\n"
            f"选股结果：\n{compact}\n"
        )
        if self.settings.llm_extra_prompt.strip():
            prompt += f"\n用户补充要求：\n{self.settings.llm_extra_prompt.strip()}"
        url = f"{self.settings.llm_base_url.rstrip('/')}/chat/completions"
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {self.settings.llm_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.settings.llm_model,
                "temperature": self.settings.llm_temperature,
                "messages": [
                    {"role": "system", "content": "你是谨慎、重视风险披露的中国股票研究助手。"},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=90,
        )
        response.raise_for_status()
        payload = response.json()
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("大模型返回格式无法识别") from exc
        if not isinstance(content, str) or not content.strip():
            raise ValueError("大模型返回了空内容")
        return content.strip()

    def test(self) -> str:
        return self.analyze([{"name": "连接测试", "strategy": "ConnectivityTest", "symbols": []}])
