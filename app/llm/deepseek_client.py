from __future__ import annotations

from openai import OpenAI

from app.llm.base import LLMClient

# DeepSeek 使用 OpenAI 兼容的 chat.completions 接口
# base_url 默认指向 https://api.deepseek.com

class DeepSeekClient(LLMClient):
    def __init__(self, api_key: str, base_url: str, model: str, temperature: float) -> None:
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.temperature = temperature

    @staticmethod
    def _extract_text_from_content(content: object) -> str:
        if isinstance(content, str):
            return content.strip()

        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
            return "\n".join(parts).strip()

        return ""

    def _extract_message_content(self, message: object) -> str:
        content = self._extract_text_from_content(getattr(message, "content", ""))
        if content:
            return content

        reasoning_content = self._extract_text_from_content(
            getattr(message, "reasoning_content", "")
        )
        return reasoning_content

    def decide(self, prompt: str) -> str:
        request_kwargs = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a trading assistant. Always respond with a single valid JSON object only. No markdown, no explanation, no code blocks.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": self.temperature,
        }

        # deepseek-reasoner 为思考模型，兼容模式下不强依赖 json_object。
        if self.model != "deepseek-reasoner":
            request_kwargs["response_format"] = {"type": "json_object"}

        resp = self.client.chat.completions.create(**request_kwargs)
        return self._extract_message_content(resp.choices[0].message)
