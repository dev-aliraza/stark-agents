from .llm_providers.litellm import LiteLLM
from .llm_providers.provider import (
    LLMProvider,
    LITELLM, OPENAI, ANTROPIC
)

def init_llm(provider: str) -> LLMProvider:
    if provider == LITELLM:
        return LiteLLM()
