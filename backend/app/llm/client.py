"""
Model-agnostic LLM client.

The three agents call `vision_json` / `text_json` and never touch a vendor SDK.
Swapping providers is an env change (LLM_PROVIDER=azure|openai), not a code
change — this keeps the agents vendor-portable and is a deliberate design point.

Every call returns an LLMResult carrying token usage and a computed USD cost so
the orchestrator can write it straight to the cost ledger.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Optional

from openai import APIError, AzureOpenAI, OpenAI

from app.settings import settings

# Tier labels so the caller picks "vision" (expensive, doc reading) vs
# "text" (cheap, validation/routing reasoning) without knowing model names.
VISION = "vision"
TEXT = "text"


@dataclass
class LLMResult:
    text: str
    model: str
    prompt_tokens: int
    output_tokens: int
    cost_usd: float

    def json(self) -> dict:
        """Parse the model output as JSON, tolerating ```json fences."""
        raw = self.text.strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        return json.loads(raw)


class LLMClient:
    def __init__(self) -> None:
        self.provider = settings.provider
        if self.provider == "azure":
            self._client = AzureOpenAI(
                api_key=settings.azure_api_key,
                azure_endpoint=settings.azure_endpoint,
                api_version=settings.azure_api_version,
            )
        elif self.provider == "openai":
            self._client = OpenAI(api_key=settings.openai_api_key)
        else:
            raise ValueError(f"Unknown LLM_PROVIDER: {self.provider}")

    # -- model resolution -------------------------------------------------
    def _model(self, tier: str) -> str:
        if self.provider == "azure":
            return settings.azure_vision_deployment if tier == VISION else settings.azure_text_deployment
        return settings.openai_vision_model if tier == VISION else settings.openai_text_model

    def _cost(self, tier: str, prompt_tokens: int, output_tokens: int) -> float:
        if tier == VISION:
            return (prompt_tokens * settings.price_vision_in + output_tokens * settings.price_vision_out) / 1_000_000
        return (prompt_tokens * settings.price_text_in + output_tokens * settings.price_text_out) / 1_000_000

    # -- core call --------------------------------------------------------
    def _complete(self, tier: str, messages: list, temperature: float = 0.0) -> LLMResult:
        model = self._model(tier)
        resp = self._client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            response_format={"type": "json_object"},
        )
        usage = resp.usage
        pt = usage.prompt_tokens if usage else 0
        ot = usage.completion_tokens if usage else 0
        return LLMResult(
            text=resp.choices[0].message.content or "",
            model=model,
            prompt_tokens=pt,
            output_tokens=ot,
            cost_usd=self._cost(tier, pt, ot),
        )

    # -- public surface ---------------------------------------------------
    def text_json(self, system: str, user: str, temperature: float = 0.0) -> LLMResult:
        """Cheap text-only structured call (validation semantics, routing reasoning)."""
        return self._complete(
            TEXT,
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature,
        )

    def vision_json(
        self,
        system: str,
        user: str,
        images: list[bytes],
        mime: str = "image/png",
        temperature: float = 0.0,
    ) -> LLMResult:
        """Vision structured call — pass one image per document page."""
        content: list = [{"type": "text", "text": user}]
        for img in images:
            b64 = base64.b64encode(img).decode()
            content.append(
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}", "detail": "high"}}
            )
        return self._complete(
            VISION,
            [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            temperature,
        )


_client: Optional[LLMClient] = None


def get_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
