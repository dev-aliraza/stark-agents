from typing import Any, Dict, List, Optional, Callable
from .llm_providers import OPENAI
from .type import SkillConfig

class Agent():
    def __init__(self,
        name: str,
        model: str,
        instructions: Optional[str] = None,
        description: Optional[str] = None,
        mcp_servers: Optional[Dict[str, Any]] = [],
        function_tools: Optional[List[Callable]] = [],
        enable_web_search: Optional[bool] = False,
        sub_agents: Optional[List['Agent']] = [],
        approvals: Optional[Dict[str, Callable]]= None, # This can be use to approve tools and sub agents
        skills: Optional[List[str]] = None,
        skill_config: Optional[SkillConfig] = None,
        model_input_hook: Optional[Callable] = None,
        post_llm_hook: Optional[Callable] = None,
        iteration_end_hook: Optional[Callable] = None,
        parallel_tool_calls: Optional[bool] = None,
        thinking_level: Optional[str] = None, #["none", "low", "medium", "high"]
        llm_provider: Optional[str] = OPENAI,
        max_iterations: Optional[int] = 10,
        max_output_tokens: Optional[int] = None,
        trace_id: Optional[str] = None
    ):
        self.name = name
        self.model = model
        self.instructions = instructions
        self.description = description
        self.mcp_servers = mcp_servers
        self.function_tools = function_tools
        self.enable_web_search = enable_web_search
        self.sub_agents = sub_agents
        self.approvals = approvals
        self.skills = skills
        self.skill_config = SkillConfig() if skills and not skill_config else skill_config
        self.model_input_hook = model_input_hook
        self.post_llm_hook = post_llm_hook
        self.iteration_end_hook = iteration_end_hook
        self.parallel_tool_calls = parallel_tool_calls
        self.thinking_level = thinking_level
        self.llm_provider = llm_provider
        self.max_iterations = max_iterations
        self.max_output_tokens = max_output_tokens
        self.trace_id = trace_id

    def get_name(self) -> str:
        return self.name
    
    def get_model(self) -> str:
        return self.model
    
    def get_instructions(self) -> Optional[str]:
        return self.instructions

    def get_description(self) -> Optional[str]:
        return self.description
    
    def get_mcp_servers(self) -> Optional[Dict[str, Any]]:
        return self.mcp_servers
    
    def get_function_tools(self) -> Optional[List[Callable]]:
        return self.function_tools
    
    def get_enable_web_search(self) -> Optional[bool]:
        return self.enable_web_search
    
    def get_sub_agents(self) -> Optional[List['Agent']]:
        return self.sub_agents
    
    def get_approvals(self) -> Optional[Dict[str, Callable]]:
        return self.approvals
    
    def get_skills(self) -> Optional[List[str]]:
        return self.skills
    
    def get_skill_config(self) -> Optional[SkillConfig]:
        return self.skill_config
    
    def get_model_input_hook(self) -> Optional[Callable]:
        return self.model_input_hook
    
    def get_post_llm_hook(self) -> Optional[Callable]:
        return self.post_llm_hook
    
    def get_iteration_end_hook(self) -> Optional[Callable]:
        return self.iteration_end_hook
    
    def get_parallel_tool_calls(self) -> Optional[bool]:
        return self.parallel_tool_calls
    
    def get_thinking_level(self) -> Optional[str]:
        return self.thinking_level
    
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
                "description": agent.get_description()
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

    async def execute(self,
        runner_instance,
        agent_name,
        input: List[Dict[str, Any]],
        stream: bool = False,
        stream_type_prefix: str = ""
    ):
        agent = self.agent_name_map[agent_name]
        if not stream:
            yield await self.subagent_execution(
                runner_instance,
                agent,
                input,
                stream
            ).__anext__(); return
        async for event in self.subagent_execution(
            runner_instance,
            agent,
            input,
            stream,
            stream_type_prefix=stream_type_prefix
        ):
            yield event
    
    @classmethod
    async def subagent_execution(
        cls,
        runner_instance,
        agent: Agent,
        input: List[Dict[str, Any]],
        stream: bool = False,
        stream_type_prefix: str = ""
    ):
        input.pop()
        if not stream:
            yield await runner_instance.run_sub_agent(
                agent,
                input,
                stream=stream,
                stream_type_prefix=stream_type_prefix
            ).__anext__(); return
        
        async for event in runner_instance.run_sub_agent(
            agent,
            input,
            stream=stream,
            stream_type_prefix=stream_type_prefix
        ):
            yield event