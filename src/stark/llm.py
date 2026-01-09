from .llm_providers.litellm import LiteLLM
from .llm_providers.provider import LLMProvider


def init_llm(provider: str) -> LLMProvider:
    return LiteLLM(provider)
