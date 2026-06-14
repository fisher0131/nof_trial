from __future__ import annotations

from openai import OpenAI

from app.llm.base import LLMClient


class OpenAIClient(LLMClient):
    def __init__(self, api_key: str, base_url: str, model: str, temperature: float) -> None:
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.temperature = temperature

    def decide(self, prompt: str) -> str:
        resp = self.client.responses.create(
            model=self.model,
            input=prompt,
            temperature=self.temperature,
        )
        return resp.output_text.strip()
