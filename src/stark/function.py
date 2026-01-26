import json, inspect, asyncio, functools
from typing import List, Callable, Dict, Any

class FunctionToolManager:
    def __init__(self, function_tools: List[Callable]):
        self.function_tools = function_tools
        self.func_name_map = {}
        self.tools = self.__load_tools()

    def __is_class_instance(self, obj):
        # Returns True for instances of user-defined classes
        # Returns False for built-in types (int, list, str, function, etc.) or classes themselves
        return hasattr(obj, '__class__') and not callable(obj) and not isinstance(obj, type)

    def __load_tools(self) -> List[Dict]:
        tools = []
        class_instance = None
        for function_tool in self.function_tools:
            func_args = {}
            if isinstance(function_tool, functools.partial):
                func_args = function_tool.keywords.copy() if function_tool.keywords else {}
                function_tool = function_tool.func

            elif self.__is_class_instance(function_tool):
                class_instance = function_tool
                function_tool = function_tool.__class__

            if inspect.isclass(function_tool):
                if not class_instance:
                    class_instance = function_tool()
                class_name = function_tool.__name__
                methods_list = inspect.getmembers(function_tool, inspect.isfunction)
                for name, func in methods_list:
                    if hasattr(func, 'get_json_schema'):
                        tool_func_def = json.loads(func.get_json_schema())
                        tool_func_def["name"] = class_name + "___" + tool_func_def["name"]
                        tools.append({
                            "type": "function",
                            "function": tool_func_def
                        })
                        self.func_name_map[tool_func_def["name"]] = {
                            "type": "class_instance",
                            "function_name": name,
                            "class_instance": class_instance,
                            "args": func_args
                        }

            elif (inspect.isfunction(function_tool) or inspect.ismethod(function_tool)) and callable(function_tool):
                if hasattr(function_tool, 'get_json_schema'):
                    tool_func_def = json.loads(function_tool.get_json_schema())
                    tool_func_def["name"] = "st___" + tool_func_def["name"]
                    tools.append({
                        "type": "function",
                        "function": tool_func_def
                    })
                    self.func_name_map[tool_func_def["name"]] = {
                        "type": "function",
                        "function": function_tool,
                        "args": func_args
                    }
                
        return tools
    
    def get_tools(self) -> List[Dict]:
        return self.tools

    async def call_tool(self, tool_name: str, arguments: Any):
        tool_details = self.func_name_map.get(tool_name, None)
        tool_func = None
        if tool_details:
            if tool_details["type"] == "function":
                tool_func = tool_details["function"]
            elif tool_details["type"] == "class_instance":
                tool_class_instance = tool_details["class_instance"]
                tool_func_name = tool_details["function_name"]
                tool_func = getattr(tool_class_instance, tool_func_name)
            
            arguments.update(tool_details["args"]) if isinstance(arguments, dict) else None
            
            if tool_func:
                if inspect.iscoroutinefunction(tool_func):
                    return await tool_func(**arguments)
                return tool_func(**arguments)
        
        return "Tool call didn't happen"
    
    def is_function_tool(self, tool_name):
        if tool_name in self.func_name_map:
            return True
        return False

