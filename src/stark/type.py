from typing import List, Dict, Any
from pydantic import BaseModel


class ModelSreamResponse(BaseModel):
    content: str
    tool_calls: List
    message: Dict[str, Any]

class RunResponse(BaseModel):
    result: List[Dict[str, Any]]
    iterations: int
    max_iterations_reached: bool = False

class IterationData(BaseModel):
    iterations: int
    has_tool_calls: bool

class StreamEvent(BaseModel):
    type: str
    data: Any
    data_type: str = "none"

class ToolCallResponse(BaseModel):
    role: str
    tool_call_id: str
    content: Any