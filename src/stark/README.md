# Stark

A powerful Python SDK for building AI agents with support for MCP servers, function tools, and hierarchical sub-agents.

## Features

- 🤖 **Multi-LLM Support**: Built-in support for multiple LLM providers via LiteLLM
- 🔧 **MCP Server Integration**: Connect to Model Context Protocol (MCP) servers for extended capabilities
- 🛠️ **Function Tools**: Define custom Python functions as tools for your agents
- 🌳 **Hierarchical Agents**: Create complex agent hierarchies with sub-agents
- 📡 **Streaming Support**: Real-time streaming of agent responses and tool calls
- 🔄 **Async/Sync APIs**: Both synchronous and asynchronous execution modes
- 📊 **Iteration Control**: Configurable maximum iterations to prevent infinite loops

## Installation

```bash
pip install stark-agents
```

## Quick Start

### Basic Agent

```python
from stark import Agent, Runner

agent = Agent(
    name="Assistant",
    instructions="You are a helpful assistant",
    model="claude-sonnet-4-5"
)

result = Runner(agent).run(input=[{"role": "user", "content": "Hello!"}])
print(result)
```

### Agent with MCP Servers

```python
import os
from stark import Agent, Runner

mcp_servers = {
    "slack": {
        "command": "uvx",
        "args": ["mcp-slack"],
        "env": {
            "SLACK_BOT_TOKEN": os.environ.get("SLACK_BOT_TOKEN", "")
        }
    }
}

agent = Agent(
    name="Slack-Agent",
    instructions="You can interact with Slack",
    model="claude-sonnet-4-5",
    mcp_servers=mcp_servers
)

result = Runner(agent).run(input=[{"role": "user", "content": "Send a message to #general"}])
```

### Agent with Function Tools

```python
import json
from stark import Agent, Runner

def search_database(input: str):
    """
    {
        "description": "Search the database for information",
        "parameters": {
            "properties": {
                "query": {
                    "description": "Search query",
                    "type": "string"
                }
            },
            "required": ["query"],
            "type": "object"
        }
    }
    """
    # Your function implementation
    return json.dumps({"results": ["item1", "item2"]})

agent = Agent(
    name="Search-Agent",
    instructions="You can search the database",
    model="claude-sonnet-4-5",
    function_tools=[search_database]
)

result = Runner(agent).run(input=[{"role": "user", "content": "Search for users"}])
```

### Hierarchical Sub-Agents

```python
from stark import Agent, Runner

# Define sub-agents
delivery_agent = Agent(
    name="Delivery-Agent",
    description="Handles pizza delivery",
    instructions="Confirm delivery details and provide tracking",
    model="claude-sonnet-4-5"
)

pizza_agent = Agent(
    name="Pizza-Agent",
    description="Handles pizza preparation",
    instructions="Prepare the pizza and call delivery agent",
    model="claude-sonnet-4-5",
    sub_agents=[delivery_agent]
)

# Main agent with sub-agents
master_agent = Agent(
    name="Master-Agent",
    instructions="Coordinate pizza orders using available agents",
    model="claude-sonnet-4-5",
    sub_agents=[pizza_agent]
)

result = Runner(master_agent).run(
    input=[{"role": "user", "content": "I want to order a pepperoni pizza"}]
)

# Access sub-agent responses
print(result.sub_agents_response.get("Pizza-Agent"))
print(result.sub_agents_response.get("Delivery-Agent"))
```

### Streaming Responses

```python
import asyncio
from stark import Agent, Runner, RunnerStream

async def main():
    agent = Agent(
        name="Streaming-Agent",
        instructions="You are a helpful assistant",
        model="claude-sonnet-4-5"
    )

    async for event in Runner(agent).run_stream(
        input=[{"role": "user", "content": "Tell me a story"}]
    ):
        if event.type == RunnerStream.CONTENT_CHUNK:
            print(RunnerStream.data_dump(event), end="", flush=True)
        
        elif event.type == RunnerStream.TOOL_CALLS:
            print(f"\nTool calls: {RunnerStream.data_dump(event)}")
        
        elif event.type == RunnerStream.TOOL_RESPONSE:
            print(f"Tool response: {RunnerStream.data_dump(event)}")
        
        elif event.type == RunnerStream.AGENT_RUN_END:
            print(f"\nAgent finished: {RunnerStream.data_dump(event)}")

asyncio.run(main())
```

## API Reference

### Agent

The main agent class that defines the behavior and capabilities of your AI agent.

```python
Agent(
    name: str,                              # Agent name
    instructions: str,                      # System instructions/prompt
    model: str,                             # LLM model to use
    description: str = "",                  # Agent description (required for sub-agents)
    mcp_servers: Dict[str, Any] = [],      # MCP server configurations
    function_tools: List[Callable] = [],   # Custom function tools
    sub_agents: List[Agent] = [],          # Sub-agents
    parallel_tool_calls: bool = None,      # Enable parallel tool execution
    llm_provider: str = LITELLM,           # LLM provider
    max_iterations: int = 10,              # Maximum iterations
    custom_llm_provider: str = "openai",   # Custom LLM provider
    trace_id: str = None                   # Trace ID for debugging
)
```

### Runner

Executes agents and manages their lifecycle.

#### Synchronous Execution

```python
runner = Runner(agent)
result = runner.run(input=[{"role": "user", "content": "Hello"}])
```

#### Asynchronous Execution

```python
runner = Runner(agent)
result = await runner.run_async(input=[{"role": "user", "content": "Hello"}])
```

#### Streaming Execution

```python
runner = Runner(agent)
async for event in runner.run_stream(input=[{"role": "user", "content": "Hello"}]):
    # Handle events
    pass
```

### RunResponse

The response object returned by agent execution.

```python
class RunResponse:
    result: List[Dict[str, Any]]           # Complete conversation history
    iterations: int                         # Number of iterations executed
    sub_agent_result: List[Dict[str, Any]] # Sub-agent specific results
    sub_agents_response: Dict[str, Any]    # Responses from sub-agents
    max_iterations_reached: bool           # Whether max iterations was hit
```

### Stream Events

When using streaming, you'll receive different event types:

- `RunnerStream.ITER_START`: Iteration started
- `RunnerStream.CONTENT_CHUNK`: Content chunk received
- `RunnerStream.TOOL_CALLS`: Tool calls made
- `RunnerStream.TOOL_RESPONSE`: Tool response received
- `RunnerStream.ITER_END`: Iteration completed
- `RunnerStream.AGENT_RUN_END`: Agent execution finished
- `RunnerStream.MODEL_STREAM_COMPLETED`: Model streaming completed

## MCP Server Configuration

MCP servers extend agent capabilities by providing additional tools and resources.

### Stdio-based MCP Server

```python
mcp_servers = {
    "server-name": {
        "command": "uvx",              # Command to run
        "args": ["mcp-server-package"], # Arguments
        "env": {                        # Environment variables
            "API_KEY": "your-key"
        }
    }
}
```

### Multiple MCP Servers

```python
mcp_servers = {
    "jira": {
        "command": "uvx",
        "args": ["mcp-atlassian"],
        "env": {
            "JIRA_URL": os.environ.get("JIRA_URL"),
            "JIRA_USERNAME": os.environ.get("JIRA_EMAIL"),
            "JIRA_API_TOKEN": os.environ.get("JIRA_TOKEN")
        }
    },
    "slack": {
        "command": "uvx",
        "args": ["mcp-slack"],
        "env": {
            "SLACK_BOT_TOKEN": os.environ.get("SLACK_BOT_TOKEN")
        }
    }
}
```

## Function Tools

Function tools are Python functions that agents can call. They must include a JSON schema in their docstring.

### Function Tool Format

```python
def my_tool(input: str):
    """
    {
        "description": "Description of what the tool does",
        "parameters": {
            "properties": {
                "param_name": {
                    "description": "Parameter description",
                    "type": "string"
                }
            },
            "required": ["param_name"],
            "type": "object"
        }
    }
    """
    # Parse input if needed
    if isinstance(input, str):
        input = json.loads(input)
    
    # Your implementation
    result = {"status": "success"}
    
    # Return as JSON string
    return json.dumps(result)
```

## Advanced Usage

### Custom LLM Provider

```python
from stark.llms import LITELLM

agent = Agent(
    name="Custom-Agent",
    instructions="You are a helpful assistant",
    model="gpt-4",
    llm_provider=LITELLM,
    custom_llm_provider="openai"
)
```

### Parallel Tool Calls

```python
agent = Agent(
    name="Parallel-Agent",
    instructions="You can call multiple tools in parallel",
    model="claude-sonnet-4-5",
    parallel_tool_calls=True,
    function_tools=[tool1, tool2, tool3]
)
```

### Iteration Control

```python
agent = Agent(
    name="Controlled-Agent",
    instructions="You are a helpful assistant",
    model="claude-sonnet-4-5",
    max_iterations=5  # Limit to 5 iterations
)

result = Runner(agent).run(input=[{"role": "user", "content": "Hello"}])

if result.max_iterations_reached:
    print("Warning: Agent reached maximum iterations!")
```

## Best Practices

1. **Clear Instructions**: Provide clear, specific instructions to guide agent behavior
2. **Tool Descriptions**: Write detailed descriptions for function tools
3. **Error Handling**: Always wrap agent execution in try-except blocks
4. **Iteration Limits**: Set appropriate `max_iterations` to prevent infinite loops
5. **Resource Cleanup**: MCP server connections are automatically cleaned up
6. **Streaming**: Use streaming for long-running tasks to provide real-time feedback
7. **Sub-Agent Descriptions**: Always provide descriptions for sub-agents so the parent agent knows when to use them

## Error Handling

```python
from stark import Agent, Runner

try:
    agent = Agent(
        name="Error-Handling-Agent",
        instructions="You are a helpful assistant",
        model="claude-sonnet-4-5"
    )
    
    result = Runner(agent).run(
        input=[{"role": "user", "content": "Hello"}]
    )
    
except Exception as e:
    print(f"Error: {e}")
    # Handle error appropriately
```

## Requirements

Python 3.10 or higher.

## Contributing

Contributions are welcome! Please feel free to submit issues and pull requests.

## Support

For issues and questions, please open an issue on the GitHub repository.