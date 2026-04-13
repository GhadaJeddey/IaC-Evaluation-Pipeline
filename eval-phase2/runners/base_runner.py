"""
runners/base_runner.py

Abstract base class inherited by all provider runners.
Responsibilities:
  - Enforce rate limiting (interval_seconds between calls)
  - Retry logic with exponential backoff on transient failures 
  - JSON extraction from raw model response (handles fenced blocks, prose, bare JSON)
  - Normalised return dict so pipeline.py never needs to know which provider ran
"""

import json
import re
import time
import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class BaseRunner(ABC):

    def __init__(self, model_cfg: dict, api_key: str):
        """
        model_cfg : one entry from the MODELS dict in config.py
        api_key   : the provider API key (string, may be empty — validated in _call_api)
        """
        self.model_cfg  = model_cfg
        self.api_key    = api_key
        self.model_id   = model_cfg["model_id"]
        self._last_call = 0.0          # epoch timestamp of last successful call

    # ------------------------------------------------------------------ #
    # Abstract — implemented differently by each provider
    # ------------------------------------------------------------------ #

    @abstractmethod
    def _call_api(self, system_prompt: str, user_prompt: str) -> str:
        """
        Make one HTTP call to the provider.
        Returns the raw text content of the model response.
        Raises RuntimeError on non-retryable errors (auth failure, bad request).
        Raises ConnectionError / TimeoutError on retryable network issues.
        """
        ...

    # ------------------------------------------------------------------ #
    # Rate limiter
    # ------------------------------------------------------------------ #

    def _enforce_rate_limit(self) -> None:
        """
        Sleeps if the time since the last call is less than interval_seconds.
        This keeps us inside the RPM limit without a token-bucket implementation.

        Example: llama-3.3-70b has rpm_limit=30 and interval_seconds=10.
        We wait at least 10 seconds between calls -> max 6 calls/min -> safely under 30.

        Example: codestral-22b has interval_seconds=2.
        We wait at least 2 seconds -> max 30 calls/min -> well under the 60 RPM limit.
        """
        interval = self.model_cfg.get("interval_seconds", 2)
        elapsed  = time.time() - self._last_call
        if elapsed < interval:
            sleep_for = interval - elapsed
            logger.debug(f"[{self.model_id}] rate limit: sleeping {sleep_for:.2f}s")
            time.sleep(sleep_for)

    # ------------------------------------------------------------------ #
    # Public — called by pipeline.py for every scenario x model
    # ------------------------------------------------------------------ #

    def run(
        self,
        system_prompt:  str,
        user_prompt:    str,
        retry_attempts: int = 3,
        retry_delay:    int = 5,
    ) -> dict:
        """
        Calls the model with retry + rate limiting.

        Returns a normalised dict:
          raw_response : str   — full text the model returned (or None on total failure)
          parsed       : dict  — the parsed JSON object (or None if parsing failed)
          parse_error  : str   — description of parse failure (or None on success)
          latency_ms   : int   — wall-clock ms for the successful API call
          attempts     : int   — how many attempts were needed
        """
        last_error = None

        for attempt in range(1, retry_attempts + 1):
            self._enforce_rate_limit()
            t0 = time.time()
            try:
                raw = self._call_api(system_prompt, user_prompt)
                self._last_call = time.time()
                latency_ms      = int((time.time() - t0) * 1000)

                parsed, parse_error = self._parse_json(raw)

                if parse_error:
                    logger.warning(
                        f"[{self.model_id}] parse error on attempt {attempt}: {parse_error}"
                    )
                    last_error = parse_error
                    if attempt < retry_attempts:
                        time.sleep(retry_delay)
                    continue

                return {
                    "raw_response": raw,
                    "parsed":       parsed,
                    "parse_error":  None,
                    "latency_ms":   latency_ms,
                    "attempts":     attempt,
                }

            except RuntimeError as exc:
                # Non-retryable: auth error, invalid request, model not found
                logger.error(f"[{self.model_id}] non-retryable error: {exc}")
                return {
                    "raw_response": None,
                    "parsed":       None,
                    "parse_error":  str(exc),
                    "latency_ms":   0,
                    "attempts":     attempt,
                }

            except Exception as exc:
                # Retryable: network timeout, connection reset, 5xx
                last_error = str(exc)
                logger.warning(
                    f"[{self.model_id}] attempt {attempt}/{retry_attempts} failed: {exc}"
                )
                if attempt < retry_attempts:
                    time.sleep(retry_delay * attempt)

        return {
            "raw_response": None,
            "parsed":       None,
            "parse_error":  f"All {retry_attempts} attempts failed. Last: {last_error}",
            "latency_ms":   0,
            "attempts":     retry_attempts,
        }

    # ------------------------------------------------------------------ #
    # JSON parser — handles every common model output style
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_json(text: str) -> tuple[Any, str | None]:
        """
        Extracts a JSON object from the model raw text response.

        Tries four strategies in order:

          1. Direct parse
             Model returned clean JSON with no wrapping — ideal case,
             happens reliably when response_format json_object is supported.

          2. ```json ... ``` fenced block
             Model ignored the JSON-only instruction and wrapped its output
             in a markdown code block with a language tag.

          3. ``` ... ``` generic fenced block
             Same as above but without the language tag.

          4. First { ... } in prose
             Model added a preamble like "Here is my analysis:" before the JSON.
             We walk character by character counting brace depth to find the
             complete object, then parse it.

        Returns (parsed_object, None) on success.
        Returns (None, error_message) on failure.
        """
        if not text or not text.strip():
            return None, "Empty response from model"

        stripped = text.strip()

        # Strategy 1: direct parse
        try:
            return json.loads(stripped), None
        except json.JSONDecodeError:
            pass

        # Strategy 2 & 3: fenced code block
        fenced = re.search(r"```(?:json)?\s*\n?([\s\S]+?)```", stripped)
        if fenced:
            try:
                return json.loads(fenced.group(1).strip()), None
            except json.JSONDecodeError:
                pass

        # Strategy 4: find first complete { ... } block in prose
        brace_start = stripped.find("{")
        if brace_start != -1:
            depth  = 0
            in_str = False
            escape = False
            for i, ch in enumerate(stripped[brace_start:], start=brace_start):
                if escape:
                    escape = False
                    continue
                if ch == "\\" and in_str:
                    escape = True
                    continue
                if ch == '"' and not escape:
                    in_str = not in_str
                    continue
                if in_str:
                    continue
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = stripped[brace_start : i + 1]
                        try:
                            return json.loads(candidate), None
                        except json.JSONDecodeError:
                            break

        return None, (
            f"Could not extract valid JSON from response "
            f"(length={len(text)}, preview={text[:120]!r})"
        )