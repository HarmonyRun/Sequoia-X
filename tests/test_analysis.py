from unittest.mock import MagicMock, patch

from sequoia_x.analysis import LlmAnalyzer
from sequoia_x.core.config import Settings


def test_analysis_prompt_contains_strategy_logic_and_results() -> None:
    settings = Settings(
        llm_enabled=True,
        llm_api_key="test-key",
        llm_model="test-model",
        llm_base_url="https://llm.example/v1",
    )
    response = MagicMock()
    response.json.return_value = {
        "choices": [{"message": {"content": "分析完成"}}],
    }
    with patch("sequoia_x.analysis.requests.post", return_value=response) as post:
        result = LlmAnalyzer(settings).analyze(
            [
                {
                    "strategy": "TurtleTradeStrategy",
                    "name": "海龟突破",
                    "description": "收盘价突破前 20 个交易日高点。",
                    "symbols": ["600519"],
                }
            ]
        )

    assert result == "分析完成"
    payload = post.call_args.kwargs["json"]
    prompt = payload["messages"][1]["content"]
    assert "收盘价突破前 20 个交易日高点" in prompt
    assert "600519" in prompt
