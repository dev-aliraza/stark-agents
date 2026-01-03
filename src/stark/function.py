import json
from typing import List, Callable, Dict, Any

class FunctionToolManager:
    def __init__(self, function_tools: List[Callable]):
        self.function_tools = function_tools
        self.func_name_map = {}
        self.tools = self.__load_tools()

    def __is_valid_json(self, json_str):
        try:
            json.loads(json_str)
            return True
        except json.decoder.JSONDecodeError:
            return False

    def __load_tools(self) -> List[Dict]:
        tools = []
        for function_tool in self.function_tools:
            if callable(function_tool) and self.__is_valid_json(function_tool.__doc__):
                toof_def = json.loads(function_tool.__doc__)
                toof_def["name"] = function_tool.__name__
                tools.append({
                    "type": "function",
                    "function": toof_def
                })
                self.func_name_map[function_tool.__name__] = function_tool
        return tools
    
    def get_tools(self) -> List[Dict]:
        return self.tools

    def call_tool(self, tool_name: str, arguments: Any):
        func = self.func_name_map[tool_name]
        return func(arguments)
    
    def is_function_tool(self, tool_name):
        if tool_name in self.func_name_map:
            return True
        return False

