import logging, json, asyncio, sys
from typing import List, Dict, Any, Callable, AsyncIterator
from .agent import Agent
from .llm import init_llm
from .llm_providers.provider import LLMProvider
from .tool import Tool
from .type import (
    Stream, RunContext, ToolCallResponse, IterationData, ModelOutput
)

class RunnerStream:

    @classmethod
    def iteration_start(cls, data: int, type_prefix: str = "") -> Stream.Event:
        return Stream.event(type=type_prefix+Stream.ITER_START, data=data, data_type="int")

    @classmethod
    def tool_response(cls, data: ToolCallResponse, type_prefix: str = "") -> Stream.Event:
        return Stream.event(type=type_prefix+Stream.TOOL_RESPONSE, data=data, data_type="BaseModel")
    
    @classmethod
    def iteration_end(cls, data: IterationData, type_prefix: str = "") -> Stream.Event:
        return Stream.event(type=type_prefix+Stream.ITER_END, data=data, data_type="BaseModel")
    
    @classmethod
    def agent_run_end(cls, data: RunContext, type_prefix: str = "") -> Stream.Event:
        return Stream.event(type=type_prefix+Stream.AGENT_RUN_END, data=data, data_type="BaseModel")
    
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
        self.stream = False
        self.stream_type_prefix = ""
    
    def __set_agent_instructions(self, messages: List[Dict], system_prompt: str):
        if self.tool.has_skills():
            system_prompt = f"""Don't generate any arguments for the tools start with `skill___`
---
{(system_prompt or "")}
"""

        if not system_prompt:
            return messages
        
        if len(messages) > 0 and messages[0].get("role") == "system":
            messages[0]["content"] = system_prompt
        else:
            messages.insert(0, {"role": "system", "content": system_prompt})
        
        return messages
    
    async def __input_hook(self, hook: Callable, run_response: RunContext) -> RunContext:
        if not hook:
            return run_response
        return hook(run_response.model_copy(deep=True))
    
    async def __execution_response(
        self,
        run_context: RunContext,
        max_iterations_reached = False
    ) -> RunContext | AsyncIterator[Stream.Event]:
        run_context.output = run_context.messages[-1]["content"] if "content" in run_context.messages[-1] else "No Output"
        if max_iterations_reached:
            run_context.max_iterations_reached = True
        await self.tool.close_mcp_manager()
        if self.stream:
            return RunnerStream.agent_run_end(run_context, type_prefix=self.stream_type_prefix)
        else:
            return run_context

    async def __execute(
        self, input: List[Dict[str, Any]],
        input_hook: Callable = None,
        iteration_end_hook: Callable = None
    ) -> AsyncIterator[RunContext] | AsyncIterator[Stream.Event]:
        # Set system prompt for the model input
        input = self.__set_agent_instructions(input, self.agent.get_instructions())
        run_context = RunContext(messages=input, iterations=0)

        while run_context.iterations < self.agent.get_max_iterations():
            run_context.iterations += 1

            if self.stream:
                yield RunnerStream.iteration_start(run_context.iterations, type_prefix=self.stream_type_prefix)

            hook_response = await self.__input_hook(input_hook, run_context)
            if not isinstance(hook_response, RunContext):
                run_context.error = "Input hook response is not the RunContext type"
                yield await self.__execution_response(run_context); return
            
            # Run the LLM
            provider: LLMProvider = init_llm(self.agent.get_llm_provider())
            llm_response = await provider.run_async(
                model=self.agent.get_model(),
                messages=run_context.messages,
                tools=self.tool.get_tools(),
                stream=self.stream,
                parallel_tool_calls = self.agent.get_parallel_tool_calls(),
                reasoning_effort = self.agent.get_thinking_level(),
                max_tokens=self.agent.get_max_output_tokens(),
                trace_id=self.agent.get_trace_id()
            )

            model_output: ModelOutput = None
            if self.stream:
                # Consume the stream and emit events for clients
                async for stream_event in provider.stream_response(llm_response, self.stream_type_prefix):
                    if stream_event.type == (self.stream_type_prefix+Stream.MODEL_STREAM_COMPLETED):
                        model_output = stream_event.data
                    else:
                        yield stream_event
            else:
                # Parse LLM async (non-stream) response
                model_output = provider.response(llm_response)
            
            run_context.messages.append(model_output.model_dump(exclude_defaults=True))

            iteration_data = IterationData(
                iterations=run_context.iterations,
                has_tool_calls=bool(model_output.tool_calls)
            )

            logging.info(
                f"Iteration {run_context.iterations}: Received response - "
                f"content length: {len(model_output.content)} chars, tool_calls: {len(model_output.tool_calls)}"
            )

            # If no tools return by LLM means agent is done working
            if not model_output.tool_calls:
                logging.info(f"No tool calls made. Agent finished after {run_context.iterations} iterations.")
                if self.stream:
                    yield RunnerStream.iteration_end(iteration_data, type_prefix=self.stream_type_prefix)
                yield await self.__execution_response(run_context); return
            
            # Call tools and its event return by LLM
            tool_responses: List[ToolCallResponse] = []
            async for tool_calls_event in self.tool.tool_calls(model_output.tool_calls, run_context):
                if isinstance(tool_calls_event, List) and all(isinstance(item, ToolCallResponse) for item in tool_calls_event):
                    tool_responses = tool_calls_event
                else:
                    yield tool_calls_event

            # Collect tools response
            for tool_response in tool_responses:
                run_context.messages.append(tool_response.model_dump())
                if self.stream:
                    yield RunnerStream.tool_response(tool_response, type_prefix=self.stream_type_prefix)

            # Filter the response before the next agent iteration
            # Usecases: To stop agent conditionally or persist the output somewhere
            if iteration_end_hook and not iteration_end_hook(run_context):
                run_context.error = "loop_output_hook returned had an error"
                yield await self.__execution_response(run_context); return

            if self.stream:
                # Yield iteration end event
                yield RunnerStream.iteration_end(iteration_data, type_prefix=self.stream_type_prefix)

        # Maximum agent iteration exhausted
        yield await self.__execution_response(
            run_context,
            max_iterations_reached=True
        ); return

    def is_stream(self):
        return self.stream

    async def run_stream(
        self,
        input: List[Dict[str, Any]],
        input_hook: Callable = None,
        iteration_end_hook: Callable = None,
        stream_type_prefix: str = ""
    ) -> AsyncIterator[Stream.Event]:
        try:
            self.stream = True
            self.stream_type_prefix = stream_type_prefix
            self.tool = await Tool(self).init_tools(self.agent)
            async for event in self.__execute(
                input=input,
                input_hook=input_hook,
                iteration_end_hook=iteration_end_hook
            ):
                yield event
        except Exception as e:
            if self.tool:
                await self.tool.close_mcp_manager()
            raise

    async def run_async(
        self,
        input: List[Dict[str, Any]],
        input_hook: Callable = None,
        iteration_end_hook: Callable = None
    ) -> RunContext:
        try:
            # If caller function is 'run_sub_agent', its a sub agent call
            if (sys._getframe(1).f_code.co_name) == 'run_sub_agent':
                self.is_sub_agent = True
            self.tool = await Tool(self).init_tools(self.agent)
            return await self.__execute(
                input=input,
                input_hook=input_hook,
                iteration_end_hook=iteration_end_hook
            ).__anext__()
        except Exception as e:
            if self.tool:
                await self.tool.close_mcp_manager()
            raise

    def run(
        self,
        input: List[Dict[str, Any]],
        input_hook: Callable = None,
        iteration_end_hook: Callable = None
    ) -> RunContext:
        try:
            return asyncio.run(
                self.run_async(
                    input=input,
                    input_hook=input_hook,
                    iteration_end_hook=iteration_end_hook
                )
            )
        except Exception as e:
            raise

    @classmethod
    async def run_sub_agent(
        cls,
        agent: Agent,
        input: List[Dict[str, Any]],
        stream: bool = False,
        stream_type_prefix: str = ""
    ):
        if not stream:
            yield await cls(agent).run_async(input=input); return
        
        async for event in cls(agent).run_stream(input=input, stream_type_prefix=stream_type_prefix):
            yield event
        
