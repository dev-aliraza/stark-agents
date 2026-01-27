from .llm_providers.litellm import LiteLLM
from .llm_providers.provider import LLMProvider

class LLM:
    @classmethod
    def init(cls, provider: str) -> LLMProvider:
        return LiteLLM(provider)

    @classmethod
    async def close_clients(cls):
        await LiteLLM.close_clients()
