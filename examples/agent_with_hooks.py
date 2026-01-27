import asyncio
from stark import Agent, Runner, RunContext, ModelOutput, logger
from stark.llm_providers import GEMINI

def post_llm_hook_func(model_output: ModelOutput, **kwargs):
    logger.info(model_output)
    logger.info(kwargs)
    return model_output

def ittr_end_hook_func(run_context: RunContext):
    logger.info(run_context)

async def main_async():
    agent = Agent(
        name="hook-agent",
        model="gemini-2.5-flash",
        llm_provider=GEMINI,
        instructions="Respond to user query nicely",
        post_llm_hook=post_llm_hook_func,
        iteration_end_hook=ittr_end_hook_func
    )

    result = await Runner(agent).run_async(input=[{"role": "user", "content": "Who is elon musk?"}])
    logger.info(result)

asyncio.run(main_async())
