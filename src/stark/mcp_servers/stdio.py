import shutil
from typing import Dict, Any, List
from contextlib import AsyncExitStack
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import Tool

class StdioMCP():
    """
    Manages multiple persistent MCP server connections.
    """
    def __init__(self):
        self.sessions: ClientSession = None
        self._exit_stack = AsyncExitStack()

    def __format_tools_for_input(self, tools: List[Tool]) -> List:
        tools_output: List[Any] = []
        for tool in tools:
            tools_output.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.inputSchema
                    if hasattr(tool, "inputSchema")
                    else {"type": "object", "properties": {}},
                },
            })
        return tools_output

    async def connect_server(self, name:str, config: Dict[str, Any], _exit_stack: AsyncExitStack):
        """
        Spawns multiple servers and keeps their sessions active.
        
        Args:
            server_configs: Dict where key is server_name and value is a dict
                            containing 'command', 'args', and optional 'env'.
        """
        command = config.get("command")
        args = config.get("args", [])
        env = config.get("env", None)

        # Check if command exists (e.g., 'python', 'npx')
        if not shutil.which(command):
            print(f"⚠️  Warning: Command '{command}' not found. Skipping {name}.")
            return None

        print(f"🔌 Connecting to {name} via {command}...")

        server_params = StdioServerParameters(
            command=command,
            args=args,
            env=env
        )

        try:
            stdio_transport = await _exit_stack.enter_async_context(
                stdio_client(server_params)
            )
            read, write = stdio_transport

            # 2. Create the session
            session = await _exit_stack.enter_async_context(
                ClientSession(read, write)
            )
                
            # 3. Initialize the protocol
            await session.initialize()

            # 4. List tools
            self.tools = self.__format_tools_for_input((await session.list_tools()).tools)
            self.session = session

            print(f"✅ {name} connected and initialized.")
            return _exit_stack

        except Exception as e:
            print(f"❌ Failed to connect to {name}: {e}")
            # Clean up on error
            raise

    async def call_tool(self, tool_name: str, arguments: dict = None):
        """
        Calls a tool on a specific preserved session.
        """
        result = await self.session.call_tool(tool_name, arguments or {})
        return result

    def get_tools(self):
        """Helper to see what tools are available on a server."""
        return self.tools

    def get_session(self):
        """Helper to see what tools are available on a server."""
        return self.session
