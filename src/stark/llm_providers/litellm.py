import os, litellm
from typing import List, Dict, Any, AsyncIterator
from .provider import LLMProvider, ProviderSream
from ..type import ProviderResponse, Stream

class LiteLLM(LLMProvider):
    def __init__(self, provider):
        self.api_base = os.environ.get("LITELLM_BASE_URL", None)
        self.api_key = os.environ.get("LITELLM_API_KEY", None)
        self.provider = provider

    def run(self, model: str, messages: List=[], tools: List=[], **kwargs):
        metadata: Dict[str, Any] = {}
        metadata["trace_id"] = kwargs.get("trace_id", None)
        parallel_tool_calls = kwargs.get("parallel_tool_calls", None)

        return litellm.completion(
            model=(self.provider + "/" + model),
            messages=messages,
            tools=tools,
            parallel_tool_calls=parallel_tool_calls,
            api_base=self.api_base,
            api_key=self.api_key,
            metadata=metadata
        )
    
    async def run_async(self, model: str, messages: List=[], tools: List=[], **kwargs):
        metadata: Dict[str, Any] = {}
        metadata["trace_id"] = kwargs.get("trace_id", None)
        parallel_tool_calls = kwargs.get("parallel_tool_calls", None)

        return await litellm.acompletion(
            model=(self.provider + "/" + model),
            messages=messages,
            tools=tools,
            stream=False,
            parallel_tool_calls=parallel_tool_calls,
            api_base=self.api_base,
            api_key=self.api_key,
            metadata=metadata
        )

    async def run_stream(self, model: str, messages: List=[], tools: List=[], **kwargs):
        metadata: Dict[str, Any] = {}
        metadata["trace_id"] = kwargs.get("trace_id", None)
        parallel_tool_calls = kwargs.get("parallel_tool_calls", None)

        return await litellm.acompletion(
            model=(self.provider + "/" + model),
            messages=messages,
            tools=tools,
            stream=True,
            parallel_tool_calls=parallel_tool_calls,
            api_base=self.api_base,
            api_key=self.api_key,
            metadata=metadata
        )
    
    def response(self, response) -> ProviderResponse:
        provider_response = ProviderResponse(content="", tool_calls=[], message={"role": "assistant"})

        if hasattr(response, "choices") and len(response.choices) > 0:
            res = response.choices[0].message

            if hasattr(res, "content") and res.content:
                provider_response.content += res.content

            if hasattr(res, "tool_calls") and res.tool_calls:
                for tool_call in res.tool_calls:
                    provider_response.tool_calls.append({
                        "id": tool_call.id,
                        "type": "function",
                        "function": {
                            "name": tool_call.function.name
                            if hasattr(tool_call.function, "name")
                            else "",
                            "arguments": tool_call.function.arguments
                            if hasattr(tool_call.function, "arguments")
                            else "",
                        },
                    })

        if provider_response.content:
            provider_response.message["content"] = provider_response.content
        
        if provider_response.tool_calls:
            provider_response.message["tool_calls"] = provider_response.tool_calls

        return provider_response
    
    async def stream_response(self, response)  -> AsyncIterator[Stream.Event]:
        provider_response = ProviderResponse(content="", tool_calls=[], message={"role": "assistant"})

        async for chunk in response:
            if hasattr(chunk, "choices") and len(chunk.choices) > 0:
                delta = chunk.choices[0].delta

                if hasattr(delta, "content") and delta.content:
                    provider_response.content += delta.content
                    yield ProviderSream.content_chunk(delta.content)

                if hasattr(delta, "tool_calls") and delta.tool_calls:
                    for tool_call in delta.tool_calls:
                        if tool_call.index >= len(provider_response.tool_calls):
                            provider_response.tool_calls.append({
                                "id": tool_call.id,
                                "type": "function",
                                "function": {
                                    "name": tool_call.function.name
                                    if hasattr(tool_call.function, "name")
                                    else "",
                                    "arguments": tool_call.function.arguments
                                    if hasattr(tool_call.function, "arguments")
                                    else "",
                                },
                            })
                        else:
                            if hasattr(tool_call.function, "arguments"):
                                provider_response.tool_calls[tool_call.index]["function"][
                                    "arguments"
                                ] += tool_call.function.arguments
                    
                    # Yield tool calls update
                    yield ProviderSream.tool_calls(provider_response.tool_calls)

        # Only add content if there is actual content
        if provider_response.content:
            provider_response.message["content"] = provider_response.content

        # Add tool calls if present
        if provider_response.tool_calls:
            provider_response.message["tool_calls"] = provider_response.tool_calls

        # Yield final complete response
        yield ProviderSream.provider_stream_completed(provider_response)

    
