from typing import Any, Dict, List, Optional, Callable
from .llms.provider import LITELLM

class Agent():
    def __init__(self,
        name: str,
        instructions: str,
        model: str,
        mcp_servers: Optional[Dict[str, Any]] = [],
        function_tools: Optional[List[Callable]] = [],
        parallel_tool_calls: Optional[bool] = None,
        llm_provider: Optional[str] = LITELLM,
        max_iterations: Optional[int] = 10,
        custom_llm_provider: Optional[str] = "openai",
        trace_id: Optional[str] = None
    ):
        self.name = name
        self.instructions = instructions
        self.model = model
        self.mcp_servers = mcp_servers
        self.function_tools = function_tools
        self.parallel_tool_calls = parallel_tool_calls
        self.llm_provider = llm_provider
        self.max_iterations = max_iterations
        self.custom_llm_provider = custom_llm_provider
        self.trace_id = trace_id

    def get_instructions(self) -> str:
        return self.instructions
    
    def get_model(self) -> str:
        return self.model
    
    def get_mcp_servers(self) -> Optional[Dict[str, Any]]:
        return self.mcp_servers
    
    def get_function_tools(self) -> Optional[List[Callable]]:
        return self.function_tools
    
    def get_parallel_tool_calls(self) -> Optional[bool]:
        return self.parallel_tool_calls
    
    def get_llm_provider(self) -> Optional[str]:
        return self.llm_provider
    
    def get_max_iterations(self) -> Optional[int]:
        return self.max_iterations
    
    def get_custom_llm_provider(self) -> Optional[str]:
        return self.custom_llm_provider
    
    def get_trace_id(self) -> Optional[str]:
        return self.trace_id
