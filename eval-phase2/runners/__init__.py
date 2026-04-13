"""
runners/__init__.py
Factory — returns the correct runner instance for a given model config.
"""

from .groq_runner    import GroqRunner
from .mistral_runner import MistralRunner
from .google_runner  import GoogleRunner
from .base_runner    import BaseRunner


def get_runner(model_cfg: dict, api_keys: dict) -> BaseRunner:
    """
    
    model_cfg : one entry from MODELS in config.py
    api_keys  : the API_KEYS dict from config.py
  
    """
    
    provider = model_cfg["provider"]

    if provider == "groq":
        return GroqRunner(model_cfg, api_keys.get("groq", ""))

    if provider == "mistral":
        return MistralRunner(model_cfg, api_keys.get("mistral", ""))

    if provider == "google":
        return GoogleRunner(model_cfg, api_keys.get("google", ""))

    raise ValueError(f"Unknown provider '{provider}' for model '{model_cfg['model_id']}'")