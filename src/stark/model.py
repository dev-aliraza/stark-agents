from typing import List
from .llms.provider import LITELLM
from .llms.litellm import LiteLLM


class Model():
    def __init__(self, provider):
        self.provider = provider

    async def run_async(
        self, model: str,
        messages: List=[],
        tools: List=[],
        stream: bool=False,
        **kwargs
    ):
        if self.provider == LITELLM:
            return  await LiteLLM(model).acompletion(
                messages=messages,
                tools=tools,
                stream=stream,
                **kwargs
            )
    
    def run(
        self, model: str,
        messages: List=[],
        tools: List=[],
        **kwargs
    ):
        if self.provider == LITELLM:
            return LiteLLM(model).completion(
                messages=messages,
                tools=tools,
                **kwargs
            )