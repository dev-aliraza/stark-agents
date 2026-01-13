import os, litellm
from typing import List, Dict, Any, AsyncIterator
from .provider import LLMProvider, ProviderSream
from ..type import Stream, ModelOutput, ToolCall

class LiteLLM(LLMProvider):
    def __init__(self, provider):
        self.api_base = os.environ.get("LITELLM_BASE_URL", None)
        self.api_key = os.environ.get("LITELLM_API_KEY", None)
        self.provider = provider

    async def run_async(self, model: str, messages: List=[], tools: List=[], **kwargs):
        metadata: Dict[str, Any] = {}
        if "trace_id" in kwargs:
            metadata["trace_id"] = kwargs.pop("trace_id")

        return await litellm.acompletion(
            model=model,
            messages=messages,
            tools=tools,
            api_base=self.api_base,
            api_key=self.api_key,
            metadata=metadata,
            custom_llm_provider=self.provider,
            **kwargs
        )

    def response(self, response) -> ModelOutput:
        model_output = ModelOutput(role="assistant")

        if hasattr(response, "choices") and len(response.choices) > 0:
            res = response.choices[0].message

            if hasattr(res, "content") and res.content:
                model_output.content += res.content

            if hasattr(res, "tool_calls") and res.tool_calls:
                for tool_call in res.tool_calls:
                    model_output.tool_calls.append(ToolCall(
                        id=tool_call.id,
                        type="function",
                        function={
                            "name": tool_call.function.name
                            if hasattr(tool_call.function, "name")
                            else "",
                            "arguments": tool_call.function.arguments
                            if hasattr(tool_call.function, "arguments")
                            else "",
                        }
                    ))

        return model_output
    
    async def stream_response(self, response) -> AsyncIterator[Stream.Event]:
        model_output = ModelOutput(role="assistant")

        async for chunk in response:
            if hasattr(chunk, "choices") and len(chunk.choices) > 0:
                delta = chunk.choices[0].delta

                if hasattr(delta, "content") and delta.content:
                    model_output.content += delta.content
                    yield ProviderSream.content_chunk(delta.content)

                if hasattr(delta, "tool_calls") and delta.tool_calls:
                    for tool_call in delta.tool_calls:
                        if tool_call.index >= len(model_output.tool_calls):
                            model_output.tool_calls.append(ToolCall(
                                id=tool_call.id,
                                type="function",
                                function={
                                    "name": tool_call.function.name
                                    if hasattr(tool_call.function, "name")
                                    else "",
                                    "arguments": tool_call.function.arguments
                                    if hasattr(tool_call.function, "arguments")
                                    else ""
                                }
                            ))
                        else:
                            if hasattr(tool_call.function, "arguments"):
                                model_output.tool_calls[tool_call.index].function[
                                    "arguments"
                                ] += tool_call.function.arguments
                    
                    # Yield tool calls update
                    yield ProviderSream.tool_calls(model_output.tool_calls)

        # Yield final complete response
        yield ProviderSream.model_stream_completed(model_output)

    
