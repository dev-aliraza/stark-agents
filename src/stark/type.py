from typing import List, Dict, Any
from pydantic import BaseModel

class Stream:

    #Runner Stream
    ITER_START: str = "ITERATION_START"
    TOOL_RESPONSE: str = "TOOL_RESPONSE"
    ITER_END: str = "ITERATION_END"
    AGENT_RUN_END: str = "AGENT_RUN_END"

    # Provider Stream
    CONTENT_CHUNK: str = "CONTENT_CHUNK"
    TOOL_CALLS: str = "TOOL_CALLS"
    PROVIDER_STREAM_COMPLETED: str = "PROVIDER_STREAM_COMPLETED"

    @classmethod
    def event(cls, type: str, data: Any, data_type: str = "none") -> 'Stream.Event':
        return cls.Event(type=type, data=data, data_type=data_type)

    class Event(BaseModel):
        type: str
        data: Any
        data_type: str = "none"

class ProviderResponse(BaseModel):
    content: str
    tool_calls: List
    message: Dict[str, Any]

class RunResponse(BaseModel):
    result: List[Dict[str, Any]]
    iterations: int
    sub_agent_result: List[Dict[str, Any]] = []
    sub_agents_response: Dict[str, Any] = {}
    max_iterations_reached: bool = False

class IterationData(BaseModel):
    iterations: int
    has_tool_calls: bool

class ToolCallResponse(BaseModel):
    role: str
    tool_call_id: str
    content: Any