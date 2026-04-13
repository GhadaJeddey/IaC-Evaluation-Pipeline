"""
runners/google_runner.py

Google AI Studio runner.
Covers: gemini-2.5-flash-preview-04-17.

Google's API is structurally different from OpenAI-compatible endpoints:
  - Uses generateContent instead of /chat/completions
  - System prompt goes in a separate systemInstruction field, not in messages
  - JSON mode is requested via responseMimeType = "application/json"
  - Response is nested under candidates[0].content.parts[0].text
  - No Authorization header — API key is a query parameter

Rate limits from config (free tier):
  gemini-2.5-flash : 10 RPM, 250 RPD, interval_seconds=7

The 7-second interval gives us ~8 calls/min which stays safely under 10 RPM.

"""

import logging
import requests
from .base_runner import BaseRunner

logger = logging.getLogger(__name__)

GOOGLE_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class GoogleRunner(BaseRunner):

    def _call_api(self, system_prompt: str, user_prompt: str) -> str:
        if not self.api_key:
            raise RuntimeError("GOOGLE_API_KEY is not set")

        model_name = self.model_cfg["model_id"]

        # API key is passed as a query parameter, not a header
        url = f"{GOOGLE_API_BASE}/{model_name}:generateContent?key={self.api_key}"

        payload = {
            # systemInstruction is separate from the conversation turns
            "systemInstruction": {
                "parts": [{"text": system_prompt}]
            },
            # contents is the conversation — single user turn for our eval
            "contents": [
                {
                    "role":  "user",
                    "parts": [{"text": user_prompt}]
                }
            ],
            "generationConfig": {
                "temperature":      0.0,
                "maxOutputTokens":  4096,
                # Request JSON output — Gemini will constrain its output to valid JSON.
                # Unlike OpenAI-style response_format, this is a MIME type string.
                "responseMimeType": "application/json",
            },
        }

        resp = requests.post(url, json=payload, timeout=120)

        # Gemini returns 429 for both RPM and RPD limit hits
        if resp.status_code == 429:
            raise ConnectionError(f"Gemini 429 rate limit: {resp.text[:200]}")

        # 400 with INVALID_ARGUMENT = bad request (wrong model name, bad payload)
        # This is non-retryable
        if resp.status_code == 400:
            raise RuntimeError(f"Gemini 400 bad request: {resp.text[:200]}")

        if resp.status_code in (401, 403):
            raise RuntimeError(f"Gemini auth error {resp.status_code}: {resp.text[:200]}")

        if resp.status_code != 200:
            raise ConnectionError(f"Gemini {resp.status_code}: {resp.text[:200]}")

        data = resp.json()

        # Check for content filtering block
        if data.get("promptFeedback", {}).get("blockReason"):
            block = data["promptFeedback"]["blockReason"]
            raise RuntimeError(f"Gemini blocked prompt: {block}")

        # Navigate the nested response structure
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            # finishReason = SAFETY or other non-STOP reasons produce empty candidates
            finish = (
                data.get("candidates", [{}])[0].get("finishReason", "UNKNOWN")
                if data.get("candidates") else "NO_CANDIDATES"
            )
            raise RuntimeError(
                f"Gemini unexpected response shape (finishReason={finish}): {exc}"
            )   