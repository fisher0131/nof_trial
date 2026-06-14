from __future__ import annotations

from app.llm.base import LLMClient


def create_llm_client(cfg) -> LLMClient:
    """根据 config.openai.provider 自动选择 LLM 客户端。

    支持的 provider:
        openai   — 原生 OpenAI，使用 client.responses.create
        deepseek — DeepSeek，使用 chat.completions（OpenAI 兼容接口）
                   支持模型 deepseek-chat / deepseek-reasoner
    """
    provider = getattr(cfg, "provider", "openai").lower()

    if provider == "deepseek":
        from app.llm.deepseek_client import DeepSeekClient
        return DeepSeekClient(
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            model=cfg.model,
            temperature=cfg.temperature,
        )
    else:
        from app.llm.openai_client import OpenAIClient
        return OpenAIClient(
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            model=cfg.model,
            temperature=cfg.temperature,
        )
