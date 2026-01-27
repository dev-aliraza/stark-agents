import os, litellm
from typing import List, Dict, Any, AsyncIterator
from .provider import LLMProvider, ProviderSream
from ..type import Stream, ModelOutput, ToolCall
from litellm.llms.custom_httpx.async_client_cleanup import close_litellm_async_clients, register_async_client_cleanup

class LiteLLM(LLMProvider):
    def __init__(self, provider):
        self.api_base = os.environ.get("LITELLM_BASE_URL", None)
        self.api_key = os.environ.get("LITELLM_API_KEY", None)
        self.provider = provider

    async def run_async(self, model: str, messages: List=[], tools: List=[], **kwargs):
        metadata: Dict[str, Any] = {}
        if "trace_id" in kwargs:
            metadata["trace_id"] = kwargs.pop("trace_id")
        
        if "stream" in kwargs and kwargs.get("stream"):
            kwargs["stream_options"] = {"include_usage": True}

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
        model_output = ModelOutput(
            role="assistant",
            cost=litellm.completion_cost(completion_response=response)
        )

        if hasattr(response, "choices") and len(response.choices) > 0:
            res = response.choices[0].message

            if hasattr(res, "thinking_blocks") and res.thinking_blocks:
                model_output.thinking_blocks = res.thinking_blocks

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
    
    async def stream_response(self, response, messages, type_prefix: str = "") -> AsyncIterator[Stream.Event]:
        model_output = ModelOutput(role="assistant")
        reasoning_content = ""
        signature = ""
        chunks = []
        async for chunk in response:
            chunks.append(chunk)
            if hasattr(chunk, "choices") and len(chunk.choices) > 0:
                delta = chunk.choices[0].delta

                if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                    reasoning_content += delta.reasoning_content
                    yield ProviderSream.reasoning_chunk(delta.reasoning_content, type_prefix=type_prefix)

                if (hasattr(delta, "thinking_blocks") 
                    and delta.thinking_blocks
                    and len(delta.thinking_blocks) > 0
                ):
                    latest_index = len(delta.thinking_blocks) - 1
                    if delta.thinking_blocks[latest_index].get("signature", ""):
                        signature = delta.thinking_blocks[latest_index].get("signature")

                if hasattr(delta, "content") and delta.content:
                    model_output.content += delta.content
                    yield ProviderSream.content_chunk(delta.content, type_prefix=type_prefix)

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
                    yield ProviderSream.tool_calls(model_output.tool_calls, type_prefix)

        if reasoning_content:
            thinking_block = {
                "type": "thinking",
                "thinking": reasoning_content,
            }
            if signature:
                thinking_block["signature"] = signature
            model_output.thinking_blocks.append(thinking_block)

        model_output.cost = litellm.completion_cost(
            completion_response=litellm.stream_chunk_builder(chunks, messages=messages)
        )
        # Yield final complete response
        yield ProviderSream.model_stream_completed(model_output, type_prefix); return
    
    @classmethod
    async def close_clients(cls):
        await close_litellm_async_clients()

    
