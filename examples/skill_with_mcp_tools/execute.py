import asyncio
from stark import Agent, Runner
from stark.llm_providers import ANTHROPIC

async def main():
    agent = Agent (
        name="Support-Agent",
        model="claude-sonnet-4-5",
        instructions="Use Support Tikcet skill for the user query.",
        skills=["./skills"],
        llm_provider=ANTHROPIC
    )

    result = Runner(agent).run(input=[{ "role": "user", "content": "My system is not working" }])

    print(result)

asyncio.run(main())