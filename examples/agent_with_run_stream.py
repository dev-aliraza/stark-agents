import os, asyncio, json
from stark import Agent, Runner, RunnerStream, Stream

MCP_SERVER = {
    "atlassian": {
        "command": "uvx",
        "args": ["mcp-atlassian"],
        "env": {
            "JIRA_URL": os.environ.get("JIRA_URL", ""),
            "JIRA_USERNAME": os.environ.get("JIRA_EMAIL", ""),
            "JIRA_API_TOKEN": os.environ.get("JIRA_TOKEN", "")
        }
    },
    "slack": {
        "command": "uvx",
        "args": ["mcp-slack"],
        "env": {
            "SLACK_BOT_TOKEN": os.environ.get("SLACK_BOT_TOKEN", "")
        }
    }
}

async def main_async():
    user_query = "Stark AI is not working"
    ticket_classification = "stark_issue"
    slack_channel = "#random"
    instructions = f"""### Context
You are an expert Assistant. Your role is to manage Jira Tickets and search stark logs.

### Ask
1. Create a Jira issue with following fields:
    1.1. Project Key: STARK
    1.2. Issue Type: 'Task'
    1.3. Summary: {user_query}
    1.4. Description:
        ```
        __Description:__ {user_query}
        ```
    1.5. Labels: type::{ticket_classification}
2. Transition issue to 'In-Progress' status
3. Send ticket `issue_key` to slack channel: `{slack_channel}`

### Rules
1. Don't output anything else other than the JSON string.
    1.1. Correct Output Example: {{"issue_key": "STARK-12345"}}
    1.2. Incorrect Output Example:  ```json\\n{{\\n"issue_key": "STARK-12345"\\n}}\\n```
2. Return following key as JSON output:
    2.1. `issue_key`
3. Pass `issue_key` to slack message and replace place holder `{{ issue_key }}` with it.
"""

    try:
        agent = Agent(
            name="Jira-Support",
            instructions=instructions,
            model="claude-sonnet-4-5",
            mcp_servers=MCP_SERVER
        )

        result = Runner(agent).run_stream(input=[{ "role": "user", "content": user_query }])
        
        # Loop over events from run_streamd

        async for event in result:
            
            # Handle different event types
            if event.type == Stream.ITER_START:
                print(f"ITER_START: {RunnerStream.data_dump(event)}")

            elif event.type == Stream.CONTENT_CHUNK:
                print(f"CONTENT_CHUNK: {RunnerStream.data_dump(event)}")

            elif event.type == Stream.TOOL_CALLS:
                print(f"TOOL_CALLS: {RunnerStream.data_dump(event)}")

            elif event.type == Stream.TOOL_RESPONSE:
                print(f"TOOL_RESPONSE: {RunnerStream.data_dump(event)}")

            elif event.type == Stream.ITER_END:
                print(f"ITER_END: {RunnerStream.data_dump(event)}")

            elif event.type == Stream.AGENT_RUN_END:
                print(f"AGENT_RUN_END: {RunnerStream.data_dump(event)}")
                if event.data.max_iterations_reached:
                    print("Warning: Max iterations reached!")

    except Exception as e:
        raise

asyncio.run(main_async())
