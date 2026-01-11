import logging, json, inspect, functools
from typing import List, Dict, Any, get_type_hints, get_origin, get_args
from .mcp import MCPManager
from .function import FunctionToolManager
from .agent import Agent, SubAgentManager
from .type import ToolCallResponse, RunResponse
from .llm_providers import OPENAI, ANTHROPIC

def stark_tool(func):
    """
    Decorator to register a function as an MCP tool.
    Attaches an 'mcp_def' attribute to the function containing the tool definition.
    """
    
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)

    # --- 1. Basic Metadata ---
    tool_name = func.__name__
    # Extract description from docstring (default to empty string if None)
    tool_description = inspect.getdoc(func) or ""

    # --- 2. Type Hint Introspection ---
    type_hints = get_type_hints(func)
    sig = inspect.signature(func)
    
    properties = {}
    required_fields = []

    # Helper to map python types to JSON schema types
    def python_type_to_json_schema(py_type):
        # Handle basics
        if py_type == str:
            return {"type": "string"}
        elif py_type == int:
            return {"type": "integer"}
        elif py_type == float:
            return {"type": "number"}
        elif py_type == bool:
            return {"type": "boolean"}
        elif py_type == dict:
            return {"type": "object"}
        
        # Handle Lists (e.g., list[str])
        origin = get_origin(py_type)
        if origin is list:
            args = get_args(py_type)
            item_schema = python_type_to_json_schema(args[0]) if args else {}
            return {"type": "array", "items": item_schema}
            
        # Fallback for complex/unknown types
        return {"type": "string"}

    # --- 3. Build Properties ---
    for param_name, param in sig.parameters.items():
        # Skip 'self' or 'cls' for class methods
        if param_name in ('self', 'cls'):
            continue
            
        # Get the Python type (default to str if not annotated)
        param_type = type_hints.get(param_name, str)
        
        # Generate schema for this field
        field_schema = python_type_to_json_schema(param_type)
        
        # Add description if parsed (Optional: You could use a docstring parser here)
        # For this simple example, we don't extract per-param descriptions from docstrings
        # as that requires complex regex depending on docstring style (Google/NumPy/Sphinx).
        
        properties[param_name] = field_schema

        # Determine if required (no default value = required)
        if param.default is inspect.Parameter.empty:
            required_fields.append(param_name)

    # --- 4. Construct the MCP Tool Definition ---
    wrapper.tool_def = {
        "name": tool_name,
        "description": tool_description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required_fields
        }
    }
    
    # helper method to get the JSON easily
    wrapper.get_json_schema = lambda: json.dumps(wrapper.tool_def, indent=2)

    return wrapper

class Tool:
    def __init__(self, runner):
        self.runner = runner
        self.mcp_manager = None
        self.ft_manager = None
        self.sub_agent_manager = None
        self.tools = []
        self.sub_agents_response = {}

    async def init_tools(self, agent: Agent):
        mcp_servers = agent.get_mcp_servers()
        function_tools = agent.get_function_tools()
        sub_agents = agent.get_sub_agents()
        enable_web_search = agent.get_enable_web_search()
        if mcp_servers:
            self.mcp_manager = await MCPManager.init(mcp_servers)
            self.tools = self.tools + self.mcp_manager.get_tools()
        if function_tools:
            self.ft_manager = FunctionToolManager(function_tools)
            self.tools = self.tools + self.ft_manager.get_tools()
        if sub_agents:
            self.sub_agent_manager = SubAgentManager(sub_agents)
            self.tools = self.tools + self.sub_agent_manager.get_agents_as_tools()
        if enable_web_search:
            if agent.get_llm_provider() == OPENAI:
                self.tools.append({"type": "web_search_preview"})
            elif agent.get_llm_provider() == ANTHROPIC:
                self.tools.append({"type": "web_search_20250305", "name": "web_search", "max_uses": 5})
        return self

    def get_tools(self) -> List[Dict]:
        return self.tools

    async def close_mcp_manager(self):
        if self.mcp_manager:
            await self.mcp_manager.close_all_sessions()
    
    def get_sub_agents_response(self) -> Dict:
        return self.sub_agents_response
    
    async def tool_calls(
        self, ai_tool_calls,
        messages: List[Dict[str, Any]] = [{}]
    ) -> List[ToolCallResponse]:
        tool_responses: List[ToolCallResponse] = []
        for ai_tool_call in ai_tool_calls:
            tool_responses.append(await self.__call(ai_tool_call, messages))
        return tool_responses

    async def __call(
        self, ai_tool_call: Dict,
        messages: List[Dict[str, Any]] = [{}]
    ) -> ToolCallResponse:
        tool_name: str = ai_tool_call["function"]["name"]
        tool_call_id = ai_tool_call["id"]
        tool_result = None

        try:
            arguments = json.loads(ai_tool_call["function"]["arguments"])
        except json.JSONDecodeError as e:
            logging.error(f"Failed to parse arguments for {tool_name}: {e}")
            arguments = {}

        if (self.mcp_manager and self.mcp_manager.is_mcp_tool(tool_name)) and (self.ft_manager and self.ft_manager.is_function_tool(tool_name)):
            tool_result = f"Tool name ({tool_name}) didn't execute because same tool exist in one of the MCP servers and in one of the function tools"
            return ToolCallResponse(role="tool", tool_call_id=tool_call_id, content=tool_result)

        logging.info(f"🔧 Tool request: {tool_call_id}")
        logging.info(f"🔧 Tool name: {tool_name}")
        logging.info(f"🔧 Tool Args: {arguments}")

        if self.mcp_manager and self.mcp_manager.is_mcp_tool(tool_name):
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

        if self.ft_manager and self.ft_manager.is_function_tool(tool_name):
            tool_result = self.ft_manager.call_tool(tool_name, arguments)
            if not isinstance(tool_result, str):
                tool_result = str(tool_result)

        if self.sub_agent_manager and self.sub_agent_manager.is_agent(tool_name):
            tool_result: RunResponse = await self.sub_agent_manager.execute(self.runner, tool_name, messages)
            self.sub_agents_response.update({tool_name.removeprefix("sub_agent__"): tool_result.sub_agent_result})
            if tool_result.sub_agent_result:
                tool_result = tool_result.sub_agent_result[-1]
                if isinstance(tool_result, dict) and "content" in tool_result:
                    tool_result = tool_result["content"]
            else:
                tool_result = "Sub-Agent executed successfully (no output returned)"
            
            logging.info(tool_result)
            if not isinstance(tool_result, str):
                tool_result = str(tool_result)

        return ToolCallResponse(role="tool", tool_call_id=tool_call_id, content=tool_result)
