from abc import ABC, abstractmethod
from typing import List, AsyncIterator
from ..type import Stream, ProviderResponse

OPENAI = "openai"
ANTHROPIC = "anthropic"

class ProviderSream:
    
    @classmethod
    def content_chunk(cls, data: str) -> Stream.Event:
        return Stream.event(type=Stream.CONTENT_CHUNK, data=data, data_type="str")

    @classmethod
    def tool_calls(cls, data: List) -> Stream.Event:
        return Stream.event(type=Stream.TOOL_CALLS, data=data, data_type="List")
    
    @classmethod
    def provider_stream_completed(cls, data: ProviderResponse) -> Stream.Event:
        return Stream.event(type=Stream.PROVIDER_STREAM_COMPLETED, data=data, data_type="BaseModel")

class LLMProvider(ABC):

    @abstractmethod
    def run(self, messages: List=[], tools: List=[], **kwargs):
        pass
    
    @abstractmethod
    async def run_async(self, model: str, messages: List=[], tools: List=[], stream: bool=False, **kwargs):
        pass

    @abstractmethod
    async def run_stream(self, model: str, messages: List=[], tools: List=[], stream: bool=False, **kwargs):
        pass

    @abstractmethod
    async def response(self, response) -> ProviderResponse:
        return ProviderResponse(content="", tool_calls=[], message={})

    @abstractmethod
    async def stream_response(self, response) -> AsyncIterator[Stream.Event]:
        yield ProviderSream.provider_stream_completed(None)
