---
name: Support-Ticket
description: Skill to create Jira tickets and send ticket to support slack thread. Use this to triage the support issues.
---
# Instructions
1. **Create Jira Ticket:** Generate a ticket with the provided project_key, issue_type, summary, description and labels.
2. **Slack Reply:** Send a slack reply to a thread with the provided slack_channel, thread_timestamp and reply_message.

# Rules
- If slack thread timestamp is "NONE", don't send any slack thread reply.

# Output Requirement
- **STRICTNESS:** You must output ONLY valid JSON. 
- **NO PROSE:** Do not include introductory text, explanations, or closing remarks.

# Input Data
- project_key: 'SUPPORT'
- issue_type: 'Request'
- summary: '{user_query}'
- description:
  '''
  __Description:__ {user_query}
  __Slack Thread:__  https://{slack_team}.slack.com/archives/{channel}/p{thread_ts}
  '''
- slack_channel: '#support-channel'
- thread_timestamp: '{thread_ts}'
- reply_message: '*Jira Ticket:* https://{org_name}.atlassian.net/browse/{issue_key}'

# Expected JSON Output Schema
{"issue_key": "STRING"}