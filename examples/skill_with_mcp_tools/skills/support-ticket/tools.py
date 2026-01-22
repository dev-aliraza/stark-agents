import os

TOOLS = {
    "mcp": {
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
    },
    "function": []
}