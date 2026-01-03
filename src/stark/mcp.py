from typing import Dict, Any, List
from contextlib import AsyncExitStack
from .mcp_servers.stdio import StdioMCP

class MCPManager:
    """
    Manages multiple persistent MCP server connections.
    """

    @classmethod
    async def init(cls, mcp_servers: Dict[str, Dict[str, Any]]):
        if mcp_servers:
            instance = cls(mcp_servers)
            await instance.connect_servers()
            return instance
        return None

    def __init__(self, server_configs: Dict[str, Dict[str, Any]]):
        self.servers: Dict[str, Any |  StdioMCP] = {}
        if server_configs:
            self.server_configs = server_configs
            self.tool_to_server: Dict[str, str]= {}
            self._exit_stack = AsyncExitStack()
    
    def __map_tool_name_to_server_name(self, server_name: str, tools: List[Dict]):
        for tool in tools:
            self.tool_to_server[tool.get("function").get("name")] = server_name

    async def connect_servers(self):
        for name, config in self.server_configs.items():
            transport = config.get("transport", "stdio")
            server = StdioMCP()
            if transport == "streamable_http":
                server = None
            await server.connect_server(name, config, self._exit_stack)
            self.servers[name] = server
            self.__map_tool_name_to_server_name(name, server.get_tools())
    
    async def call_tool(self, tool_name: str, arguments: dict = None):
        if self.servers:
            return await self.servers[self.tool_to_server[tool_name]].call_tool(tool_name, arguments)
        return None

    def get_tools(self) -> List[Dict]:
        if self.servers:
            return [tool for _, server in self.servers.items() for tool in server.get_tools()]
        return []
    
    async def close_all_sessions(self):
        """Close all MCP server sessions properly."""
        await self._exit_stack.aclose()

    def is_mcp_tool(self, tool_name: str):
        if tool_name in self.tool_to_server:
            return True
        return False