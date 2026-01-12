import logging, json, asyncio, sys
from typing import List, Dict, Any, Callable
from .agent import Agent
from .llm import init_llm
from .llm_providers.provider import LLMProvider
from .tool import Tool
from .type import (
    Stream, ProviderResponse, RunResponse, ToolCallResponse, IterationData
)

class RunnerStream:

    @classmethod
    def iteration_start(cls, data: int) -> Stream.Event:
        return Stream.event(type=Stream.ITER_START, data=data, data_type="int")

    @classmethod
    def tool_response(cls, data: ToolCallResponse) -> Stream.Event:
        return Stream.event(type=Stream.TOOL_RESPONSE, data=data, data_type="BaseModel")
    
    @classmethod
    def iteration_end(cls, data: IterationData) -> Stream.Event:
        return Stream.event(type=Stream.ITER_END, data=data, data_type="BaseModel")
    
    @classmethod
    def agent_run_end(cls, data: RunResponse) -> Stream.Event:
        return Stream.event(type=Stream.AGENT_RUN_END, data=data, data_type="BaseModel")
    
    @classmethod
    def data_dump(cls, event: Stream.Event) -> str:
        if event.data_type == "int":
            return str(event.data)
        elif event.data_type == "str":
            return str(event.data)
        elif event.data_type == "List":
            return json.dumps(event.data)
        elif event.data_type == "Dict":
            return json.dumps(event.data)
        elif event.data_type == "BaseModel":
            return json.dumps(event.data.model_dump())

class Runner():
    def __init__(self,
        agent: Agent
    ):
        self.agent = agent
        self.mcp_manager = None
        self.ft_manager = None
        self.tool = None
        self.is_sub_agent = False
    
    def __set_agent_instructions(self, messages: List, system_prompt):
        if not system_prompt:
            return messages
        
        system_prompt_msg = {"role": "system", "content": system_prompt}
        if not messages or (len(messages) == 1 and messages[0].get("role") != "system"):
            messages.insert(0, system_prompt_msg)
        else:
            messages.append(system_prompt_msg)
        return messages

    async def __execute(
        self, input: List[Dict[str, Any]],
        stream: bool = False,
        input_filter: Callable = None
    ):
        # Set system prompt for the model input
        input = self.__set_agent_instructions(input, self.agent.get_instructions())
        run_response = RunResponse(result=input, iterations=0)
        # If sub agent, get the last index value of the input. It will be the system prompt in any way.
        if self.is_sub_agent and self.agent.get_instructions():
            run_response.sub_agent_result.append(input[-1])

        while run_response.iterations < self.agent.get_max_iterations():
            run_response.iterations += 1

            if stream:
                yield RunnerStream.iteration_start(run_response.iterations)

            # Filter input before sending input to the LLM call
            if input_filter:
                run_response.result = input_filter(run_response.result)
                if not isinstance(run_response.result, list) or len(run_response.result) < 1:
                    yield run_response
                    return
            
            # Run the LLM
            provider: LLMProvider = init_llm(self.agent.get_llm_provider())
            llm_response = await provider.run_async(
                model=self.agent.get_model(),
                messages=run_response.result,
                tools=self.tool.get_tools(),
                stream=stream,
                parallel_tool_calls = self.agent.get_parallel_tool_calls(),
                max_tokens=self.agent.get_max_output_tokens(),
                trace_id=self.agent.get_trace_id()
            )

            provider_response: ProviderResponse = None
            if stream:
                # Consume the stream and emit events for clients
                async for stream_event in provider.stream_response(llm_response):
                    if stream_event.type == Stream.PROVIDER_STREAM_COMPLETED:
                        provider_response = stream_event.data
                    else:
                        yield stream_event
            else:
                # Parse LLM async (non-stream) response
                provider_response = provider.response(llm_response)
            
            run_response.result.append(provider_response.message)

            iteration_data = IterationData(
                iterations=run_response.iterations,
                has_tool_calls=bool(provider_response.tool_calls)
            )

            logging.info(
                f"Iteration {run_response.iterations}: Received response - "
                f"content length: {len(provider_response.content)} chars, tool_calls: {len(provider_response.tool_calls)}"
            )

            if self.is_sub_agent:
                run_response.sub_agent_result.append(provider_response.message)

            # If no tools return by LLM means agent is done working
            if not provider_response.tool_calls:
                logging.info(f"No tool calls made. Agent finished after {run_response.iterations} iterations.")
                run_response.sub_agents_response = self.tool.get_sub_agents_response()
                await self.tool.close_mcp_manager()
                if stream:
                    # Yield agent finished event
                    yield RunnerStream.iteration_end(iteration_data)
                    yield RunnerStream.agent_run_end(run_response)
                else:
                    yield run_response
                return

            # Call tools return by LLM
            tool_responses: List[ToolCallResponse] = await self.tool.tool_calls(
                provider_response.tool_calls, run_response.result
            )

            # Collect tools response 
            for tool_response in tool_responses:
                run_response.result.append(tool_response.model_dump())
                if stream:
                    yield RunnerStream.tool_response(tool_response)

            if stream:
                # Yield iteration end event
                yield RunnerStream.iteration_end(iteration_data)

        # Maximum agent iteration exhausted
        run_response.sub_agents_response = self.tool.get_sub_agents_response()
        run_response.max_iterations_reached = True
        await self.tool.close_mcp_manager()
        if stream:
            yield RunnerStream.agent_run_end(run_response)
            return
        yield run_response

    async def run_stream(
        self,
        input: List[Dict[str, Any]],
        input_filter: Callable = None
    ):
        try:
            self.tool = await Tool(self).init_tools(self.agent)
            async for event in self.__execute(input=input, stream=True, input_filter=input_filter):
                yield event
        except Exception as e:
            if self.tool:
                await self.tool.close_mcp_manager()
            raise

    async def run_async(
        self,
        input: List[Dict[str, Any]],
        input_filter: Callable = None
    ):
        try:
            # If caller function is 'run_sub_agent', its a sub agent call
            if (sys._getframe(1).f_code.co_name) == 'run_sub_agent':
                self.is_sub_agent = True
            self.tool = await Tool(self).init_tools(self.agent)
            async for exec_result in self.__execute(input=input, input_filter=input_filter):
                return exec_result
        except Exception as e:
            if self.tool:
                await self.tool.close_mcp_manager()
            raise

    def run(
        self,
        input: List[Dict[str, Any]],
        input_filter: Callable = None
    ):
        try:
            return asyncio.run(self.run_async(input=input, input_filter=input_filter))
        except Exception as e:
            raise

    @classmethod
    async def run_sub_agent(cls, agent: Agent, input: List[Dict[str, Any]]):
        return await cls(agent).run_async(input=input)
