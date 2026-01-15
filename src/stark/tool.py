import logging, json, inspect, re, copy
from typing import List, Dict
from .mcp import MCPManager
from .function import FunctionToolManager
from .skill import Skill
from .agent import Agent, SubAgentManager
from .type import ToolCallResponse, RunContext, ToolCall
from .llm_providers import OPENAI, ANTHROPIC

class Tool:
    def __init__(self, runner):
        self.runner = runner
        self.mcp_manager = None
        self.ft_manager = None
        self.subagent_manager = None
        self.tools = []
        self.subagents_response = {}
        self.subagents_messages = []
        self.agent = None
        self.skill = None

    async def init_tools(self, agent: Agent):
        self.agent = agent
        mcp_servers = agent.get_mcp_servers()
        function_tools = agent.get_function_tools()
        sub_agents = agent.get_sub_agents()
        skills = agent.get_skills()
        enable_web_search = agent.get_enable_web_search()
        if mcp_servers:
            self.mcp_manager = await MCPManager.init(mcp_servers)
            self.tools = self.tools + self.mcp_manager.get_tools()
        if function_tools:
            self.ft_manager = FunctionToolManager(function_tools)
            self.tools = self.tools + self.ft_manager.get_tools()
        if sub_agents:
            self.subagent_manager = SubAgentManager(sub_agents)
            self.tools = self.tools + self.subagent_manager.get_agents_as_tools()
        if skills:
            self.skill = Skill(self.agent)
            self.tools = self.tools + self.skill.get_metadata_as_tools()
        if enable_web_search:
            if agent.get_llm_provider() == OPENAI:
                self.tools.append({"type": "web_search_preview"})
            elif agent.get_llm_provider() == ANTHROPIC:
                self.tools.append({"type": "web_search_20250305", "name": "web_search", "max_uses": 5})
        return self

    def get_tools(self) -> List[Dict]:
        return self.tools
    
    def has_skils(self) -> bool:
        return True if self.skill else False

    async def close_mcp_manager(self):
        if self.mcp_manager:
            await self.mcp_manager.close_all_sessions()
    
    def get_subagents_messages(self) -> Dict :
        return self.subagents_messages

    def get_subagents_response(self) -> Dict:
        return self.subagents_response
    
    async def __is_tool_approved(self, tool_name: str, arguments: Dict) -> bool:
        approvals = self.agent.get_approvals()
        if not approvals:
            return True
        
        tool_for_approval = {tool_name: v for k, v in approvals.items() if re.search(fr"{k.lower()}", tool_name.lower())}
        if not tool_for_approval:
            return True
        
        if len(tool_for_approval) > 1:
            print("===== According to regex search, multiple tools are found for approval =====")
            return False
        
        try:
            approval_func = tool_for_approval[tool_name]
            args = {"tool_name": tool_name, "arguments": arguments}
            if inspect.iscoroutinefunction(approval_func):
                return await approval_func(**args)
            else:
                return approval_func(**args)
        except Exception as e:
            print(f"Exception Occur: {str(e)}")
            return False

    async def tool_calls(
        self,
        ai_tool_calls: List[ToolCall],
        runner_context: RunContext = None
    ) -> List[ToolCallResponse]:
        tool_responses: List[ToolCallResponse] = []
        for ai_tool_call in ai_tool_calls:
            tool_responses.append(await self.__call(ai_tool_call, runner_context))
        return tool_responses

    async def __call(
        self,
        ai_tool_call: ToolCall,
        runner_context: RunContext = None
    ) -> ToolCallResponse:
        tool_name: str = ai_tool_call.function["name"]
        tool_call_id = ai_tool_call.id
        tool_result = None

        try:
            arguments = json.loads(ai_tool_call.function["arguments"])
        except json.JSONDecodeError as e:
            logging.error(f"Failed to parse arguments for {tool_name}: {e}")
            arguments = {}

        if (self.mcp_manager and self.mcp_manager.is_mcp_tool(tool_name)) and (self.ft_manager and self.ft_manager.is_function_tool(tool_name)):
            tool_result = f"Tool name ({tool_name}) didn't execute because same tool exist in one of the MCP servers and in one of the function tools"
            return ToolCallResponse(role="tool", tool_call_id=tool_call_id, content=tool_result)

        logging.info(f"🔧 Tool request: {tool_call_id}")
        logging.info(f"🔧 Tool name: {tool_name}")
        logging.info(f"🔧 Tool Args: {arguments}")

        if not (await self.__is_tool_approved(tool_name, arguments)):
            tool_result = f"Tool name ({tool_name}) didn't execute because the approval got rejected by the user. Just respond that the action to {{ Add Action Name }} was not approved by user and no additional information or question"
            return ToolCallResponse(role="tool", tool_call_id=tool_call_id, content=tool_result)

        if self.mcp_manager and self.mcp_manager.is_mcp_tool(tool_name):
            tool_result = await self.__call_mcp_tool(tool_name, arguments)

        elif self.ft_manager and self.ft_manager.is_function_tool(tool_name):
            tool_result = await self.__call_function_tool(tool_name, arguments)

        elif self.subagent_manager and self.subagent_manager.is_agent(tool_name) and runner_context.messages:
            tool_result = await self.__call_subagent(tool_name, runner_context)

        elif self.skill and self.skill.is_skill(tool_name) and runner_context.messages:
            tool_result = await self.__call_skill(tool_name, runner_context)

        return ToolCallResponse(role="tool", tool_call_id=tool_call_id, content=tool_result)
    
    async def __call_mcp_tool(self, tool_name: str, arguments) -> str:
        try:
            tool_error = None
            result = await self.mcp_manager.call_tool(tool_name, arguments)

            if result.content:
                if hasattr(result.content[0], "text"):
                    tool_result = result.content[0].text
                elif hasattr(result.content[0], "data"):
                    tool_result = str(result.content[0].data)
                else:
                    tool_result = str(result.content[0])
            else:
                tool_result = ""
                
            # Check if result is an error from wrong server
            if tool_result and "Unknown tool:" in tool_result:
                logging.warning(
                    f"Tool {tool_name} not available"
                    f"trying next server"
                )

            logging.info(f"Tool {tool_name} result length: {len(tool_result)} chars")
        except Exception as e:
            tool_error = str(e)
            logging.info(
                f"Tool {tool_name} raised exception: {e}"
            )

        # Ensure tool_result is never empty
        if tool_result is None or (isinstance(tool_result, str) and not tool_result.strip()):
            if tool_result is None:
                error_msg = (
                    f"Tool {tool_name} not found"
                    if not tool_error
                    else f"Tool error: {tool_error}"
                )
                logging.error(f"Failed to execute {tool_name}: {error_msg}")
                tool_result = json.dumps({"error": error_msg})
            else:
                # Empty result - provide a default message
                tool_result = "Tool executed successfully (no output returned)"
                logging.warning(f"Tool {tool_name} returned empty result")

        tool_result = tool_result if isinstance(tool_result, str) else json.dumps(tool_result)
        if not tool_result.strip():
            tool_result = "Tool executed successfully (no output returned)"

        return tool_result
    
    async def __call_function_tool(self, tool_name: str, arguments) -> str:
        tool_result = self.ft_manager.call_tool(tool_name, arguments)
        if not isinstance(tool_result, str):
            tool_result = str(tool_result)
            
        return tool_result

    async def __call_subagent(self, tool_name: str, runner_context: RunContext) -> str:
        subagent_repsonse: RunContext = await self.subagent_manager.execute(self.runner, tool_name, copy.deepcopy(runner_context.messages))
        runner_context.subagents_messages[tool_name.removeprefix("sub_agent__")] = subagent_repsonse.messages
        if subagent_repsonse.output:
            tool_result = subagent_repsonse.output
        else:
            tool_result = "Sub-Agent executed successfully (no output returned)"

        logging.info(tool_result)
        if not isinstance(tool_result, str):
            tool_result = str(tool_result)
        
        runner_context.subagents_response[tool_name.removeprefix("sub_agent__")] = tool_result
            
        return tool_result
    
    async def __call_skill(self, tool_name: str, runner_context: RunContext) -> str:
        skill_subagent = self.skill.get_skill_subagent(tool_name)
        agent_response: RunContext = await SubAgentManager.subagent_execution(self.runner, skill_subagent, copy.deepcopy(runner_context.messages))
        if agent_response.output:
            tool_result = agent_response.output
        else:
            tool_result = "Sub-Agent executed successfully (no output returned)"
        
        logging.info(tool_result)
        if not isinstance(tool_result, str):
            tool_result = str(tool_result)
        
        return tool_result
