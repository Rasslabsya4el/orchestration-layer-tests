"""The token-meter's model table: names for commit trailers and the codex.sale credit
multipliers (the owner's table, 2026-09-26), kept in this one file (requirement 2)."""

from __future__ import annotations

MODEL_NAMES = {
    "gpt-5.6-terra": "GPT-5.6 Terra",
    "deepseek-flash": "DeepSeek V4.1 Flash",
    "gpt-6-luna": "GPT-6 Luna",
}

# codex.sale credit multiplier per model (the owner, 2026-09-26): what one request against that
# model costs against the shared codex.sale budget, relative to a multiplier-1 request.
CREDIT_MULTIPLIERS = {
    "deepseek-flash": 0.3,
    "gpt-6-luna": 0.9,
    "gpt-5.6-terra": 1.5,
}


def credit_multiplier(model: str) -> float:
    return CREDIT_MULTIPLIERS.get(model, 1.0)


def model_name(model: str) -> str:
    return MODEL_NAMES.get(model, model)
