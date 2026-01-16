from abc import ABC, abstractmethod
from typing import List, AsyncIterator
from ..type import Stream, ModelOutput

OPENAI = "openai"
ANTHROPIC = "anthropic"
GEMINI="gemini"

class ProviderSream:
    
    @classmethod
    def content_chunk(cls, data: str) -> Stream.Event:
        return Stream.event(type=Stream.CONTENT_CHUNK, data=data, data_type="str")

    @classmethod
    def tool_calls(cls, data: List) -> Stream.Event:
        return Stream.event(type=Stream.TOOL_CALLS, data=data, data_type="List")
    
    @classmethod
    def model_stream_completed(cls, data: ModelOutput) -> Stream.Event:
        return Stream.event(type=Stream.MODEL_STREAM_COMPLETED, data=data, data_type="BaseModel")

class LLMProvider(ABC):
    
    @abstractmethod
    async def run_async(self, model: str, messages: List=[], tools: List=[], **kwargs):
        pass

    @abstractmethod
    async def response(self, response) -> ModelOutput:
        return ModelOutput(role="assistant")

    @abstractmethod
    async def stream_response(self, response) -> AsyncIterator[Stream.Event]:
        yield ProviderSream.model_stream_completed(None)
