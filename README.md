# Stark Agents

A powerful Python ADK for building model agnostic AI agents with the support for MCP servers, function tools, hierarchical sub-agents, and advanced execution control.

## Features

- 🧠 **Model Agnostic**: Built on top of LiteLLM, allowing you to use 100+ LLMs (OpenAI, Anthropic, Gemini, DeepSeek, Ollama, etc.) interchangeably.
- 🔧 **Native MCP Support**: First-class support for Model Context Protocol (MCP) servers. Connect any MCP server to extend agent capabilities instantly.
- 📚 **Markdown-based Skills**: Define agent skills in simple Markdown files (`SKILL.md`) with natural language. No complex coding required to add new capabilities.
- 🛠️ **Function Tools & Schema**: Write standard Python functions or classes. Stark automatically generates JSON schemas and handles execution.
- 👥 **Hierarchical Agents**: Build complex workflows with a main agent delegating tasks to specialized sub-agents.
- 🪝 **Lifecycle Hooks**: Granular control with `model_input_hook`, `post_llm_hook`, and `iteration_end_hook` to modify behavior at every step.
- 📡 **Streaming Support**: Real-time streaming of content, tool calls, and state updates for responsive UI applications.
- 🔍 **Web Search**: Built-in support for web search capabilities with citation tracking.
- 🛡️ **Human-in-the-loop**: Comprehensive approval systems for sensitive tool calls and actions.

## Comparison with other ADKs

| Feature | Stark Agents | LangChain | AutoGen | CrewAI |
| :--- | :--- | :--- | :--- | :--- |
| **Core Philosophy** | **Lightweight, Model Agnostic, Skill-based** | Chain-based, Extensive Integrations | Conversational, Multi-agent | Role-playing, Process-oriented |
| **Model Support** | **Truly Agnostic (via LiteLLM)** | Extensive (via integrations) | Extensive | Open Source focus |
| **Skill Definition** | **Markdown / Natural Language** | (No Skills Support) | (No Skills Support) | (No Skills Support) |
| **Complexity** | **Low (Pythonic, Minimal)** | High (Steep learning curve) | Medium | Medium |
| **MCP Support** | **Native First-Class** | Via community/addons | Via extensions | Via extensions |
| **Agent Definition** | **Single Class (Agent)** | Multiple Chains/Agents | ConversationalAgent | Agent Role Class |

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
print(result.result[-1]["content"])
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

result = Runner(agent).run(
    input=[{"role": "user", "content": "Send a message to #general"}]
)
```

### Agent with Function Tools

#### Using the `@stark_tool` Decorator (Recommended)

The `@stark_tool` decorator automatically generates JSON schemas from your function signatures:

```python
from stark import Agent, Runner, stark_tool

@stark_tool
def search_database(query: str, limit: int = 10) -> str:
    """Search the database for information"""
    # Your function implementation
    results = ["item1", "item2"]
    return f"Found {len(results)} results for '{query}'"

@stark_tool
def get_user_info(user_id: int, include_details: bool = False) -> str:
    """Retrieve user information from the database"""
    return f"User {user_id} details"

agent = Agent(
    name="Search-Agent",
    instructions="You can search the database and get user info",
    model="claude-sonnet-4-5",
    function_tools=[search_database, get_user_info]
)

result = Runner(agent).run(
    input=[{"role": "user", "content": "Search for users named John"}]
)
```

#### Using Class-Based Tools

You can also organize related tools into classes:

```python
from stark import Agent, Runner, stark_tool

class DatabaseTools:
    def __init__(self, db_connection):
        self.db = db_connection
    
    @stark_tool
    def search(self, query: str, limit: int = 10) -> str:
        """Search the database"""
        return f"Search results for: {query}"
    
    @stark_tool
    def insert(self, table: str, data: dict) -> str:
        """Insert data into a table"""
        return f"Inserted into {table}"

# Pass the class instance
db_tools = DatabaseTools(db_connection="my_db")

agent = Agent(
    name="DB-Agent",
    instructions="You can interact with the database",
    model="claude-sonnet-4-5",
    function_tools=[db_tools]
)
```

#### Built-in Code Tools

Stark includes a comprehensive `CodeTool` class for file operations:

```python
from stark import Agent, Runner
from stark.tools import CodeTool

code_tool = CodeTool(workspace_dir="./my_project")

agent = Agent(
    name="Code-Agent",
    instructions="You can read, write, and manage files",
    model="claude-sonnet-4-5",
    function_tools=[code_tool]
)

result = Runner(agent).run(
    input=[{"role": "user", "content": "Create a new Python file called app.py"}]
)
```

### Skills System

Stark supports a unique "Skills" system where you can define reusable agent capabilities using markdown files.

#### Directory Structure

Create a `skills` directory with subdirectories for each skill:

```
skills/
  ├── python_expert/
  │   └── SKILL.md
  └── data_analyst/
      └── SKILL.md
```

#### The SKILL.md Format

Each skill is defined in a `SKILL.md` file with YAML frontmatter:

```markdown
---
name: python_expert
description: A skill that provides Python programming expertise
---

You are an expert Python programmer. You follow PEP 8 standards.
When writing code, always include type hints and docstrings.
```

#### Loading Skills

```python
from stark import Agent, Runner
from stark.type import SkillConfig

# Basic skill loading
agent = Agent(
    name="Dev-Agent",
    instructions="You are a senior developer.",
    model="claude-sonnet-4-5",
    skills=["./skills/python_expert"]  # Path to the skill folder
)

# Advanced: Customize skill execution with SkillConfig
skill_config = SkillConfig(
    model="gpt-4o",                    # Use a different model for skills
    llm_provider="openai",             # Specify LLM provider for skills
    max_iterations=50,                 # Increase iteration limit for skills
    max_output_tokens=32000,           # Set max output tokens
    parallel_tool_calls=True,          # Enable parallel tool execution
    thinking_level="high",             # Set reasoning level for thinking models
    enable_web_search=True             # Enable web search for skills
)

agent = Agent(
    name="Dev-Agent",
    instructions="You are a senior developer.",
    model="claude-sonnet-4-5",
    skills=["./skills/python_expert"],
    skill_config=skill_config
)
```

### Advanced Skills Usage with Custom Tools

Skills can have their own MCP servers and function tools by creating a `tools.py` file in the skill directory:

**Directory Structure:**
```
skills/
  └── web_scraper/
      ├── SKILL.md
      └── tools.py
```

**tools.py:**
```python
from stark import stark_tool
import os

@stark_tool
def parse_html(html: str) -> str:
    """Parse HTML content and extract text"""
    # Your implementation
    return "Parsed content"

# Define tools dictionary
TOOLS = {
    "mcp": {
        "browser": {
            "command": "uvx",
            "args": ["mcp-server-browser"],
            "env": {}
        }
    },
    "function": [
        parse_html
    ]
}
```

When this skill is invoked, it will have access to:
- The built-in `CodeTool` (automatically included)
- Any MCP servers defined in `TOOLS["mcp"]`
- Any function tools defined in `TOOLS["function"]`

### Reasoning Models

Stark supports reasoning (or "thinking") models like OpenAI's O1 series. You can control the reasoning effort:

```python
agent = Agent(
    name="Thinking-Agent",
    instructions="Solve this complex logic puzzle",
    model="o1",
    thinking_level="high"  # Options: "none", "low", "medium", "high"
)
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
from stark import Agent, Runner, RunnerStream, Stream

async def main():
    agent = Agent(
        name="Streaming-Agent",
        instructions="You are a helpful assistant",
        model="claude-sonnet-4-5"
    )

    async for event in Runner(agent).run_stream(
        input=[{"role": "user", "content": "Tell me a story"}]
    ):
        if event.type == Stream.CONTENT_CHUNK:
            print(RunnerStream.data_dump(event), end="", flush=True)
        
        elif event.type == Stream.TOOL_CALLS:
            print(f"\nTool calls: {RunnerStream.data_dump(event)}")
        
        elif event.type == Stream.TOOL_RESPONSE:
            print(f"Tool response: {RunnerStream.data_dump(event)}")
        
        elif event.type == Stream.ITER_START:
            print(f"\n--- Iteration {RunnerStream.data_dump(event)} ---")
        
        elif event.type == Stream.ITER_END:
            print(f"\n--- Iteration Complete ---")
        
        elif event.type == Stream.AGENT_RUN_END:
            print(f"\nAgent finished: {RunnerStream.data_dump(event)}")

asyncio.run(main())
```

### Web Search

Enable web search capabilities for your agents:

```python
from stark import Agent, Runner
from stark.llm_providers import OPENAI, ANTHROPIC, GEMINI

# OpenAI web search
openai_agent = Agent(
    name="Research-Agent",
    instructions="You can search the web for information",
    model="gpt-4o",
    llm_provider=OPENAI,
    enable_web_search=True
)

# Anthropic web search
anthropic_agent = Agent(
    name="Research-Agent",
    instructions="You can search the web for information",
    model="claude-sonnet-4-5",
    llm_provider=ANTHROPIC,
    enable_web_search=True
)

# Gemini web search
gemini_agent = Agent(
    name="Research-Agent",
    instructions="You can search the web for information",
    model="gemini-1.5-pro",
    llm_provider=GEMINI,
    enable_web_search=True
)

result = Runner(openai_agent).run(
    input=[{"role": "user", "content": "What's the latest news about AI?"}]
)
```

### Tool Approvals

Implement approval workflows for sensitive operations:

```python
from stark import Agent, Runner

def approve_file_deletion(tool_name: str, arguments: dict) -> bool:
    """Approve file deletion operations"""
    file_path = arguments.get("path", "")
    print(f"Approve deletion of {file_path}? (y/n)")
    return input().lower() == 'y'

async def approve_api_call(tool_name: str, arguments: dict) -> bool:
    """Async approval for API calls"""
    print(f"Approve API call to {tool_name}? (y/n)")
    return input().lower() == 'y'

agent = Agent(
    name="Controlled-Agent",
    instructions="You can perform file operations",
    model="claude-sonnet-4-5",
    function_tools=[file_tool],
    approvals={
        "delete": approve_file_deletion,  # Matches tool names containing "delete"
        "api_.*": approve_api_call,       # Regex pattern for API tools
    }
)
```

### Input Filtering

Filter or modify input before sending to the LLM:

```python
from stark import Agent, Runner

def filter_sensitive_data(messages: list) -> list:
    """Remove sensitive information from messages"""
    filtered = []
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            # Remove credit card numbers, etc.
            content = content.replace("1234-5678-9012-3456", "[REDACTED]")
            filtered.append({"role": msg["role"], "content": content})
        else:
            filtered.append(msg)
    return filtered

agent = Agent(
    name="Secure-Agent",
    instructions="You are a helpful assistant",
    model="claude-sonnet-4-5",
    model_input_hook=filter_sensitive_data
)

result = Runner(agent).run(
    input=[{"role": "user", "content": "My card is 1234-5678-9012-3456"}]
)
```

## API Reference

### Agent

The main agent class that defines the behavior and capabilities of your AI agent.

```python
Agent(
    name: str,                                    # Agent name (required)
    instructions: str,                            # System instructions/prompt (required)
    model: str,                                   # LLM model to use (required)
    description: str = "",                        # Agent description (required for sub-agents)
    mcp_servers: Dict[str, Any] = [],            # MCP server configurations
    function_tools: List[Callable] = [],         # Custom function tools or class instances
    enable_web_search: bool = False,             # Enable web search capabilities
    sub_agents: List[Agent] = [],                # Sub-agents for delegation
    approvals: Dict[str, Callable] = None,       # Tool approval functions (regex patterns)
    skills: List[str] = None,                    # List of paths to skill directories
    skill_config: SkillConfig = None,            # Configuration for skill execution
    model_input_hook: Callable = None,           # Function to modify input before LLM call
    post_llm_hook: Callable = None,              # Function to modify response after LLM call
    iteration_end_hook: Callable = None,         # Function to run at end of iteration (except the last iteration - Not useful for the agents with only 1 iteration)
    parallel_tool_calls: bool = None,            # Enable parallel tool execution
    thinking_level: str = None,                  # Reasoning effort: "none", "low", "medium", "high"
    llm_provider: str = OPENAI,                  # LLM provider (OPENAI, ANTHROPIC, GEMINI)
    max_iterations: int = 10,                    # Maximum iterations before stopping
    max_output_tokens: int = None,               # Maximum tokens in response
    trace_id: str = None                         # Trace ID for debugging
)
```

### Runner

Executes agents and manages their lifecycle.

#### Synchronous Execution

```python
runner = Runner(agent)
result = runner.run(
    input=[{"role": "user", "content": "Hello"}]
)
```

#### Asynchronous Execution

```python
runner = Runner(agent)
result = await runner.run_async(
    input=[{"role": "user", "content": "Hello"}]
)
```

#### Streaming Execution

```python
runner = Runner(agent)
async for event in runner.run_stream(
    input=[{"role": "user", "content": "Hello"}]
):
    # Handle events
    pass
```

### SkillConfig

Configuration class for customizing skill execution behavior.

```python
from stark.type import SkillConfig

skill_config = SkillConfig(
    model: Optional[str] = None,              # Model to use for skill execution (defaults to agent's model)
    llm_provider: Optional[str] = None,       # LLM provider for skills (defaults to agent's provider)
    max_iterations: int = 100,                # Maximum iterations for skill execution
    max_output_tokens: int = 64000,           # Maximum output tokens for skills
    parallel_tool_calls: bool = True,         # Enable parallel tool calls in skills
    thinking_level: Optional[str] = None,     # Reasoning level: \"none\", \"low\", \"medium\", \"high\"
    enable_web_search: bool = False           # Enable web search for skills
)
```

**Use Case**: When you want skills to use different models or have different execution parameters than the main agent.

### RunContext

The response object returned by agent execution.

```python
class RunContext:
    messages: List[Dict[str, Any]]              # Complete conversation history
    output: str                                 # Final output of the agent
    iterations: int                             # Number of iterations executed
    subagents_messages: Dict[str, List]         # Messages from all sub-agents (typically empty for Single Agent or Master Agent)
    subagents_response: Dict[str, Any]          # Responses from all sub-agents (typically empty for Single Agent)
    error: Optional[str]                        # Error message if execution failed
    max_iterations_reached: bool                # Whether max iterations was hit
    run_cost: float                             # Total cost of the run in USD
```

### Stream Events

When using streaming, you'll receive different event types:

**Runner Events:**
- `Stream.ITER_START`: Iteration started (data: iteration number)
- `Stream.TOOL_RESPONSE`: Tool response received (data: ToolCallResponse)
- `Stream.ITER_END`: Iteration completed (data: IterationData)
- `Stream.AGENT_RUN_END`: Agent execution finished (data: RunContext)

**Provider Events (from LLM):**
- `Stream.REASONING_CHUNK`: Reasoning/thinking content chunk (for models with thinking capability)
- `Stream.CONTENT_CHUNK`: Content chunk received (data: string)
- `Stream.TOOL_CALLS`: Tool calls made (data: list of tool calls)
- `Stream.MODEL_STREAM_COMPLETED`: Provider streaming completed (data: ProviderResponse)

### Utility Classes

#### Util

Helper utilities for common operations:

```python
from stark import Util

# 1. Parse JSON from LLM responses (handles markdown code blocks)
data = Util.load_json('```json\n{"key": "value"}\n```')
# Returns: {"key": "value"}

# 2. Create partial functions with pre-filled arguments
from functools import partial

def my_approval_func(tool_name: str, args: dict, user_id: int):
    print(f"User {user_id}: Approve {tool_name}?")
    return True

# Pass function with pre-filled user_id
approval_with_user = Util.pass_function_with_args(my_approval_func, user_id=123)
# Now approval_with_user only needs tool_name and args
```

#### RunnerStream

Helper methods for working with stream events:

```python
from stark import RunnerStream

# Create stream events
event = RunnerStream.iteration_start(1)
event = RunnerStream.tool_response(tool_response)
event = RunnerStream.iteration_end(iteration_data)
event = RunnerStream.agent_run_end(run_response)

# Dump event data to string
data_str = RunnerStream.data_dump(event)
```

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

### Using the `@stark_tool` Decorator

The `@stark_tool` decorator automatically generates JSON schemas from Python type hints:

```python
from stark import stark_tool
from typing import List

@stark_tool
def my_tool(
    query: str,                    # Required parameter
    limit: int = 10,               # Optional with default
    tags: List[str] = None,        # Optional list
    include_metadata: bool = False # Optional boolean
) -> str:
    """
    Description of what the tool does.
    This docstring becomes the tool description.
    """
    # Your implementation
    return "result"
```

**Supported Types:**
- `str` → string
- `int` → integer
- `float` → number
- `bool` → boolean
- `dict` → object
- `List[T]` → array with items of type T

### Class-Based Tools

Organize related tools into classes:

```python
from stark import stark_tool

class MyTools:
    def __init__(self, config):
        self.config = config
    
    @stark_tool
    def tool_one(self, param: str) -> str:
        """First tool description"""
        return f"Result: {param}"
    
    @stark_tool
    def tool_two(self, value: int) -> str:
        """Second tool description"""
        return f"Value: {value}"

# Use the class instance
tools = MyTools(config="my_config")
agent = Agent(
    name="Agent",
    instructions="Instructions",
    model="claude-sonnet-4-5",
    function_tools=[tools]
)
```

### Built-in CodeTool

The `CodeTool` class provides comprehensive file and shell operations:

```python
from stark.tools import CodeTool

code_tool = CodeTool(workspace_dir="./project")

# Available methods:
# - read(path, encoding='utf-8')
# - write(path, content, create_dirs=True)
# - update(path, search, replace, count=-1)
# - delete(path, recursive=False)
# - create_directory(path, parents=True)
# - list_directory(path=".", pattern="*", recursive=False)
# - move(source, destination)
# - copy(source, destination, recursive=True)
# - shell_exec(cmd, dir_path=None, timeout=30)
```

## Advanced Usage

### LLM Providers

```python
from stark import Agent, Runner
from stark.llm_providers import OPENAI, ANTHROPIC, GEMINI

# OpenAI
openai_agent = Agent(
    name="OpenAI-Agent",
    instructions="You are a helpful assistant",
    model="gpt-4o",
    llm_provider=OPENAI
)

# Anthropic
anthropic_agent = Agent(
    name="Anthropic-Agent",
    instructions="You are a helpful assistant",
    model="claude-sonnet-4-5",
    llm_provider=ANTHROPIC
)

# Gemini
gemini_agent = Agent(
    name="Gemini-Agent",
    instructions="You are a helpful assistant",
    model="gemini-1.5-pro",
    llm_provider=GEMINI
)
```

### Parallel Tool Calls

Enable parallel execution of multiple tools:

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

### Token Limits

Control the maximum output tokens:

```python
agent = Agent(
    name="Limited-Agent",
    instructions="You are a helpful assistant",
    model="claude-sonnet-4-5",
    max_output_tokens=1000  # Limit response to 1000 tokens
)
```

### Tracing and Debugging

Use trace IDs to track agent execution:

```python
import uuid

agent = Agent(
    name="Traced-Agent",
    instructions="You are a helpful assistant",
    model="claude-sonnet-4-5",
    trace_id=str(uuid.uuid4())
)

result = Runner(agent).run(input=[{"role": "user", "content": "Hello"}])
print(f"Trace ID: {agent.get_trace_id()}")
```

### Tool Naming Conventions

Stark automatically prefixes tool names to avoid conflicts and identify tool types:

| Tool Type | Prefix | Example |
|-----------|--------|---------|
| Function Tools | `st___` | `st___search_database` |
| Class-Based Tools | `ClassName___` | `DatabaseTools___search` |
| Sub-Agents | `sub_agent__` | `sub_agent__Delivery-Agent` |
| Skills | `skill___` | `skill___python_expert` |
| MCP Tools | (no prefix) | `slack_send_message` |

These prefixes are handled internally and you don't need to use them when defining tools. The agent automatically recognizes and routes tool calls to the appropriate handler.

### Logger

Stark includes a built-in logger for debugging:

```python
from stark import logger

# Use the logger in your code
logger.debug("Debug message")
logger.info("Info message")
logger.warning("Warning message")
logger.error("Error message")

# The logger includes file name and line numbers automatically
# Output format: YYYY-MM-DD HH:MM:SS - LEVEL - message (filename.py:123)
```

The logger is pre-configured with a StreamHandler and outputs to console with timestamps and source location.

## Best Practices

1. **Clear Instructions**: Provide clear, specific instructions to guide agent behavior
2. **Tool Descriptions**: Write detailed descriptions for function tools and sub-agents
3. **Error Handling**: Always wrap agent execution in try-except blocks
4. **Iteration Limits**: Set appropriate `max_iterations` to prevent infinite loops
5. **Resource Cleanup**: MCP server connections are automatically cleaned up
6. **Streaming**: Use streaming for long-running tasks to provide real-time feedback
7. **Sub-Agent Descriptions**: Always provide descriptions for sub-agents so the parent agent knows when to use them
8. **Type Hints**: Use type hints with `@stark_tool` for automatic schema generation
9. **Approvals**: Implement approval workflows for sensitive operations
10. **Input Filtering**: Use input filters to sanitize or modify data before LLM processing

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
    
    if result.max_iterations_reached:
        print("Warning: Maximum iterations reached")
    
except Exception as e:
    print(f"Error: {e}")
    # Handle error appropriately
```

## Examples

Check out the `examples/` directory for more comprehensive examples:

- Basic agent usage
- MCP server integration
- Function tools and class-based tools
- Hierarchical sub-agents
- Streaming responses
- Web search integration
- Tool approvals and input filtering

## Requirements

- Python 3.10 or higher
- Dependencies are automatically installed with the package

## Contributing

Contributions are welcome! Please feel free to submit issues and pull requests.

## License

See LICENSE file for details.

## Support

For issues and questions, please open an issue on the GitHub repository.