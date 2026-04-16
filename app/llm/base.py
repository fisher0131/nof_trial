from __future__ import annotations

from abc import ABC, abstractmethod


class LLMClient(ABC):
    @abstractmethod
    def decide(self, prompt: str) -> str:
        raise NotImplementedError
