import os, json
from stark import Agent, Runner

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

def stark_search(input: str):
    """
    {
        "description": "This function will search the stark logs",
        "parameters": {
            "properties": {
                "query": {
                    "description": "Query send by user to search in stark logs",
                    "title": "User Query",
                    "type": "string"
                }
            },
            "required": [
                "query"
            ],
            "type": "object"
        }
    }
    """
    try:
        if isinstance(input, str):
            input = json.loads(input)
        if "query" in input:
            return json.dumps({"result": "This is a mock function with Query"})
        return json.dumps({"result": "This is a mock function without Query"})
    except Exception as e:
        return f"Exception in calling the function: {str(e)}"

def main():
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
4. Search stark logs using following user query: {user_query}

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
            mcp_servers=MCP_SERVER,
            function_tools=[stark_search]
        )

        result = Runner(agent).run(input=[{ "role": "user", "content": user_query }])
        
        print(result)

    except Exception as e:
        raise

main()
