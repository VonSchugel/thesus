from __future__ import annotations

from typing import Protocol


class LLMProvider(Protocol):
    def planner_model_name(self) -> str: ...
    def navigator_model_name(self) -> str: ...
    def extractor_model_name(self) -> str: ...


class DefaultLLMProvider:
    def __init__(self, settings: object) -> None:
        self.settings = settings

    def planner_model_name(self) -> str:
        return getattr(self.settings, 'ollama_model_planner', 'qwen2.5:7b')

    def navigator_model_name(self) -> str:
        return getattr(self.settings, 'ollama_model_navigator', 'qwen2.5:7b')

    def extractor_model_name(self) -> str:
        return getattr(self.settings, 'ollama_model_extractor', 'qwen2.5:7b')

