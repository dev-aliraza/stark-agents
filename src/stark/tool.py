import logging, json
from typing import Any, List, Dict
from .mcp import MCPManager
from .function import FunctionToolManager
from .type import ToolCallResponse

class Tool:
    def __init__(self, 
        ai_tool_calls: List[Dict[str, Any]],
        mcp_manager: MCPManager,
        ft_manager: FunctionToolManager
    ):
        self.ai_tool_calls = ai_tool_calls
        self.mcp_manager = mcp_manager
        self.ft_manager = ft_manager
    
    async def tool_calls(self) -> List[ToolCallResponse]:
        tool_responses: List[ToolCallResponse] = []
        for ai_tool_call in self.ai_tool_calls:
            tool_responses.append(await self.__call(ai_tool_call))
        return tool_responses

    async def __call(self, ai_tool_call: Dict) -> ToolCallResponse:
        tool_name = ai_tool_call["function"]["name"]
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

                logging.info(result)

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
            logging.info(tool_result)
            if not isinstance(tool_result, str):
                tool_result = str(tool_result)

        return ToolCallResponse(role="tool", tool_call_id=tool_call_id, content=tool_result)
