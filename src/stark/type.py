from typing import List, Dict, Any, Optional
from pydantic import BaseModel

class Stream:

    #Runner Stream
    ITER_START: str = "ITERATION_START"
    TOOL_RESPONSE: str = "TOOL_RESPONSE"
    ITER_END: str = "ITERATION_END"
    AGENT_RUN_END: str = "AGENT_RUN_END"

    # Model Stream
    CONTENT_CHUNK: str = "CONTENT_CHUNK"
    TOOL_CALLS: str = "TOOL_CALLS"
    MODEL_STREAM_COMPLETED: str = "MODEL_STREAM_COMPLETED"

    @classmethod
    def event(cls, type: str, data: Any, data_type: str = "none") -> 'Stream.Event':
        return cls.Event(type=type, data=data, data_type=data_type)

    class Event(BaseModel):
        type: str
        data: Any
        data_type: str = "none"

class ToolCall(BaseModel):
    id: str
    type: str
    function: Dict[str, Any]

class ModelOutput(BaseModel):
    role: str = ""
    content: str = ""
    tool_calls: List[ToolCall] = []

class ProviderResponse(BaseModel):
    content: str
    tool_calls: List
    message: Dict[str, Any]

class RunContext(BaseModel):
    messages: List[Dict[str, Any]]
    iterations: int
    output: str = ""
    subagents_messages: Dict[str, List] = {}
    subagents_response: Dict[str, Any] = {}
    error: Optional[str] = None
    max_iterations_reached: bool = False

class IterationData(BaseModel):
    iterations: int
    has_tool_calls: bool

class ToolCallResponse(BaseModel):
    role: str
    tool_call_id: str
    content: Any