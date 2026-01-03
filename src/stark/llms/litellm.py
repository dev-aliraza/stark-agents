import os, litellm
from typing import List, Dict, Any

class LiteLLM():
    def __init__(self, model: str,):
        self.model = model
        self.base_url = os.environ.get("LITELLM_BASE_URL", "")
        self.api_key = os.environ.get("LITELLM_API_KEY", "")

    async def acompletion(self, messages: List=[], tools: List=[], stream: bool=False, **kwargs):
        metadata: Dict[str, Any] = {}
        custom_llm_provider = kwargs.get("custom_llm_provider", "openai")
        metadata["trace_id"] = kwargs.get("trace_id", None)
        parallel_tool_calls = kwargs.get("parallel_tool_calls", None)

        return await litellm.acompletion(
            model=self.model,
            messages=messages,
            tools=tools,
            stream=stream,
            parallel_tool_calls=parallel_tool_calls,
            api_base=self.base_url,
            api_key=self.api_key,
            custom_llm_provider=custom_llm_provider,
            metadata=metadata
        )

    def completion(self, messages: List=[], tools: List=[], **kwargs):
        metadata: Dict[str, Any] = {}
        custom_llm_provider = kwargs.get("custom_llm_provider", "openai")
        metadata["trace_id"] = kwargs.get("trace_id", None)
        parallel_tool_calls = kwargs.get("parallel_tool_calls", None)

        return litellm.completion(
            model=self.model,
            messages=messages,
            tools=tools,
            parallel_tool_calls=parallel_tool_calls,
            api_base=self.base_url,
            api_key=self.api_key,
            custom_llm_provider=custom_llm_provider,
            metadata=metadata
        )
