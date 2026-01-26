import json, inspect, functools
from typing import get_type_hints, get_origin, get_args

def stark_tool(func):
    """
    Decorator to register a function as an MCP tool.
    Attaches an 'mcp_def' attribute to the function containing the tool definition.
    """
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
    tool_def = {
        "name": tool_name,
        "description": tool_description,
    }
    if properties:
        tool_def.update({
            "parameters": {
                "type": "object",
                "properties": properties,
            }
        })

    if properties and required_fields:
        tool_def["parameters"]["required"] = required_fields

    if inspect.iscoroutinefunction(func):
        @functools.wraps(func)
        async def wrapper_async(*args, **kwargs):
            return await func(*args, **kwargs)
        wrapper_async.get_json_schema = lambda: json.dumps(tool_def, indent=2)
        return wrapper_async
    else:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        wrapper.get_json_schema = lambda: json.dumps(tool_def, indent=2)
        return wrapper