"""
runners/groq_runner.py

Groq API runner.
Covers: qwen3-coder-32b, llama-3.3-70b, and the NL judge (llama-3.1-8b-instant).

Groq uses the OpenAI-compatible chat completions endpoint, so the payload
structure is identical to the OpenAI SDK — role-based messages array,
response_format for JSON mode, standard max_tokens and temperature.

Rate limits from config (free tier):
  qwen3-coder-32b  : 60 RPM, 1000 RPD, interval_seconds=10
  llama-3.3-70b    : 30 RPM, 1000 RPD, interval_seconds=10
"""

import logging
import requests
from .base_runner import BaseRunner

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


class GroqRunner(BaseRunner):

    def _call_api(self, system_prompt: str, user_prompt: str) -> str:
        if not self.api_key:
            raise RuntimeError("GROQ_API_KEY is not set")

        payload = {
            "model":       self.model_cfg["model_id"],
            "max_tokens":  4096,
            "temperature": 0.0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            # Groq supports JSON mode — enforces the model outputs valid JSON.
            # This eliminates Strategy 2/3/4 in the parser for most responses.
            "response_format": {"type": "json_object"},
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type":  "application/json",
        }

        resp = requests.post(GROQ_URL, json=payload, headers=headers, timeout=120)

        # 429 = rate limit hit — retryable (base_runner will retry)
        if resp.status_code == 429:
            raise ConnectionError(f"Groq 429 rate limit: {resp.text[:200]}")

        # 401/403 = bad API key — non-retryable
        if resp.status_code in (401, 403):
            raise RuntimeError(f"Groq auth error {resp.status_code}: {resp.text[:200]}")

        # Any other non-200 — treat as retryable server error
        if resp.status_code != 200:
            raise ConnectionError(f"Groq {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        return data["choices"][0]["message"]["content"]