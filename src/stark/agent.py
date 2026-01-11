from typing import Any, Dict, List, Optional, Callable
from .llm_providers import OPENAI

class Agent():
    def __init__(self,
        name: str,
        instructions: str,
        model: str,
        description: str = "",
        mcp_servers: Optional[Dict[str, Any]] = [],
        function_tools: Optional[List[Callable]] = [],
        enable_web_search: Optional[bool] = False,
        sub_agents: Optional[List['Agent']] = [],
        parallel_tool_calls: Optional[bool] = None,
        llm_provider: Optional[str] = OPENAI,
        max_iterations: Optional[int] = 10,
        max_output_tokens: Optional[int] = None,
        trace_id: Optional[str] = None
    ):
        self.name = name
        self.instructions = instructions
        self.model = model
        self.description = description
        self.mcp_servers = mcp_servers
        self.function_tools = function_tools
        self.enable_web_search = enable_web_search
        self.sub_agents = sub_agents
        self.parallel_tool_calls = parallel_tool_calls
        self.llm_provider = llm_provider
        self.max_iterations = max_iterations
        self.max_output_tokens = max_output_tokens
        self.trace_id = trace_id

    def get_name(self) -> str:
        return self.name
    
    def get_instructions(self) -> str:
        return self.instructions
    
    def get_model(self) -> str:
        return self.model
    
    def get_description(self) -> str:
        return self.description
    
    def get_mcp_servers(self) -> Optional[Dict[str, Any]]:
        return self.mcp_servers
    
    def get_function_tools(self) -> Optional[List[Callable]]:
        return self.function_tools
    
    def get_enable_web_search(self) -> Optional[bool]:
        return self.enable_web_search
    
    def get_sub_agents(self) -> Optional[List['Agent']]:
        return self.sub_agents
    
    def get_parallel_tool_calls(self) -> Optional[bool]:
        return self.parallel_tool_calls
    
    def get_llm_provider(self) -> Optional[str]:
        return self.llm_provider
    
    def get_max_iterations(self) -> Optional[int]:
        return self.max_iterations
    
    def get_max_output_tokens(self) -> Optional[int]:
        return self.max_output_tokens
    
    def get_trace_id(self) -> Optional[str]:
        return self.trace_id

class SubAgentManager():
    def __init__(self, agents: List[Agent]):
        self.agents = agents
        self.agent_name_map = {}
        self.tools = self.__load_agents_as_tools()
    
    def __load_agents_as_tools(self) -> List[Dict]:
        tools = []
        for agent in self.agents:
            toof_def = {
                "name": "sub_agent__" + agent.get_name(),
                "description": agent.get_description(),
                "parameters": {
                    "properties": {},
                    "required": [],
                    "type": "object"
                }
            }
            tools.append({
                "type": "function",
                "function": toof_def
            })
            self.agent_name_map[toof_def["name"]] = agent
        return tools
    
    def is_agent(self, name):
        if name in self.agent_name_map:
            return True
        return False

    def get_agents_as_tools(self) -> List[Dict]:
        return self.tools

    async def execute(self, runner_instance, agent_name, input: List[Dict[str, Any]]):
        agent = self.agent_name_map[agent_name]
        input = list(input) # list copy - to avoid deleting tool call from original run response result
        input.pop()
        return await runner_instance.run_sub_agent(agent, input)