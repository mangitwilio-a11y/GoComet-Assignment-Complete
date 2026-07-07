"""Central settings, loaded once from the environment (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    provider: str = os.getenv("LLM_PROVIDER", "azure").lower()

    # Azure
    azure_api_key: str = os.getenv("AZURE_OPENAI_API_KEY", "")
    azure_endpoint: str = os.getenv("AZURE_OPENAI_ENDPOINT", "")
    azure_api_version: str = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
    azure_vision_deployment: str = os.getenv("AZURE_OPENAI_VISION_DEPLOYMENT", "gpt-4o")
    azure_text_deployment: str = os.getenv("AZURE_OPENAI_TEXT_DEPLOYMENT", "gpt-4o-mini")

    # OpenAI
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_vision_model: str = os.getenv("OPENAI_VISION_MODEL", "gpt-4o")
    openai_text_model: str = os.getenv("OPENAI_TEXT_MODEL", "gpt-4o-mini")

    # Pricing (USD per 1M tokens)
    price_vision_in: float = _f("PRICE_VISION_INPUT_PER_M", 2.50)
    price_vision_out: float = _f("PRICE_VISION_OUTPUT_PER_M", 10.00)
    price_text_in: float = _f("PRICE_TEXT_INPUT_PER_M", 0.15)
    price_text_out: float = _f("PRICE_TEXT_OUTPUT_PER_M", 0.60)

    # Guardrails
    max_retries_per_step: int = _i("MAX_RETRIES_PER_STEP", 2)
    run_budget_usd: float = _f("RUN_BUDGET_USD", 0.50)
    shipment_budget_usd: float = _f("SHIPMENT_BUDGET_USD", 2.00)
    db_path: str = os.getenv("DB_PATH", "nova.db")

    # Part 2: simulated SU inbox (watched folder trigger)
    inbox_dir: str = os.getenv("INBOX_DIR", "inbox")
    inbox_poll_seconds: float = _f("INBOX_POLL_SECONDS", 2.0)
    upload_dir: str = os.getenv("UPLOAD_DIR", "uploads")

    @property
    def configured(self) -> bool:
        if self.provider == "azure":
            return bool(self.azure_api_key and self.azure_endpoint)
        return bool(self.openai_api_key)


settings = Settings()
