"""
runners/mistral_runner.py

Mistral API runner.
Covers: codestral-22b (codestral-latest).

Mistral also uses an OpenAI-compatible chat completions endpoint,
so the payload is structurally identical to Groq. The key differences:
  - Different base URL
  - Mistral's JSON mode uses the same response_format field
  - codestral-22b has no RPD limit (None in config) — only RPM matters

Rate limits from config:
  codestral-22b : 60 RPM, no RPD limit, interval_seconds=2
"""

import logging
import requests
from .base_runner import BaseRunner

logger = logging.getLogger(__name__)

MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"


class MistralRunner(BaseRunner):

    def _call_api(self, system_prompt: str, user_prompt: str) -> str:
        if not self.api_key:
            raise RuntimeError("MISTRAL_API_KEY is not set")

        payload = {
            "model":       self.model_cfg["model_id"],
            "max_tokens":  4096,
            "temperature": 0.0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            # Mistral supports JSON mode via response_format — same as OpenAI/Groq.
            # Codestral is a code-focused model so it generally produces clean JSON
            # even without this, but explicit mode reduces parse failures.
            "response_format": {"type": "json_object"},
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type":  "application/json",
        }

        resp = requests.post(MISTRAL_URL, json=payload, headers=headers, timeout=120)

        if resp.status_code == 429:
            raise ConnectionError(f"Mistral 429 rate limit: {resp.text[:200]}")

        if resp.status_code in (401, 403):
            raise RuntimeError(f"Mistral auth error {resp.status_code}: {resp.text[:200]}")

        if resp.status_code != 200:
            raise ConnectionError(f"Mistral {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        return data["choices"][0]["message"]["content"]