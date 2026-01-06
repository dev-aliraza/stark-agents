import logging, json, asyncio, sys
from typing import List, Dict, Any
from .agent import Agent
from .model import Model
from .tool import Tool
from .type import (
    ModelSreamResponse, RunResponse, StreamEvent, ToolCallResponse, IterationData
)

class RunnerStream:

    ITER_START = "ITERATION_START"
    CONTENT_CHUNK = "CONTENT_CHUNK"
    TOOL_CALLS = "TOOL_CALLS"
    TOOL_RESPONSE = "TOOL_RESPONSE"
    ITER_END = "ITERATION_END"
    AGENT_RUN_END = "AGENT_RUN_END"
    MODEL_STREAM_COMPLETED = "MODEL_STREAM_COMPLETED"

    @classmethod
    def iteration_start(cls, data: int) -> StreamEvent:
        return StreamEvent(type=cls.ITER_START, data=data, data_type="int")
    
    @classmethod
    def content_chunk(cls, data: str) -> StreamEvent:
        return StreamEvent(type=cls.CONTENT_CHUNK, data=data, data_type="str")

    @classmethod
    def tool_calls(cls, data: List) -> StreamEvent:
        return StreamEvent(type=cls.TOOL_CALLS, data=data, data_type="List")

    @classmethod
    def tool_response(cls, data: ToolCallResponse) -> StreamEvent:
        return StreamEvent(type=cls.TOOL_RESPONSE, data=data, data_type="BaseModel")
    
    @classmethod
    def iteration_end(cls, data: IterationData) -> StreamEvent:
        return StreamEvent(type=cls.ITER_END, data=data, data_type="BaseModel")
    
    @classmethod
    def agent_run_end(cls, data: RunResponse) -> StreamEvent:
        return StreamEvent(type=cls.AGENT_RUN_END, data=data, data_type="BaseModel")
    
    @classmethod
    def model_stream_completed(cls, data: ModelSreamResponse) -> StreamEvent:
        return StreamEvent(type=cls.MODEL_STREAM_COMPLETED, data=data, data_type="BaseModel")
    
    @classmethod
    def data_dump(cls, event: StreamEvent) -> StreamEvent:
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

    async def __get_model_stream(self, response):
        model_stream_response = ModelSreamResponse(content="", tool_calls=[], message={"role": "assistant"})

        async for chunk in response:
            if hasattr(chunk, "choices") and len(chunk.choices) > 0:
                delta = chunk.choices[0].delta

                if hasattr(delta, "content") and delta.content:
                    model_stream_response.content += delta.content
                    yield RunnerStream.content_chunk(delta.content)

                if hasattr(delta, "tool_calls") and delta.tool_calls:
                    for tool_call in delta.tool_calls:
                        if tool_call.index >= len(model_stream_response.tool_calls):
                            model_stream_response.tool_calls.append({
                                "id": tool_call.id,
                                "type": "function",
                                "function": {
                                    "name": tool_call.function.name
                                    if hasattr(tool_call.function, "name")
                                    else "",
                                    "arguments": tool_call.function.arguments
                                    if hasattr(tool_call.function, "arguments")
                                    else "",
                                },
                            })
                        else:
                            if hasattr(tool_call.function, "arguments"):
                                model_stream_response.tool_calls[tool_call.index]["function"][
                                    "arguments"
                                ] += tool_call.function.arguments
                    
                    # Yield tool calls update
                    yield RunnerStream.tool_calls(model_stream_response.tool_calls)

        # Only add content if there is actual content
        if model_stream_response.content:
            model_stream_response.message["content"] = model_stream_response.content

        # Add tool calls if present
        if model_stream_response.tool_calls:
            model_stream_response.message["tool_calls"] = model_stream_response.tool_calls

        # Yield final complete response
        yield RunnerStream.model_stream_completed(model_stream_response)

    async def __stream_with_events(self, input: List[Dict[str, Any]]):
        """Internal streaming method that yields events"""
        input = self.__set_agent_instructions(input, self.agent.get_instructions())
        run_response = RunResponse(result=input, iterations=0)

        while run_response.iterations < self.agent.get_max_iterations():
            run_response.iterations += 1

            yield RunnerStream.iteration_start(run_response.iterations)

            response = await Model(self.agent.get_llm_provider()).run_async(
                model=self.agent.get_model(),
                messages=run_response.result,
                tools=self.tool.get_tools(),
                stream=True,
                parallel_tool_calls = self.agent.get_parallel_tool_calls(),
                custom_llm_provider=self.agent.get_custom_llm_provider(),
                trace_id=self.agent.get_trace_id()
            )

            # Consume the stream and emit events for each chunk
            stream_response: ModelSreamResponse = None
            async for stream_event in self.__get_model_stream(response):
                if stream_event.type == RunnerStream.MODEL_STREAM_COMPLETED:
                    stream_response = stream_event.data
                else:
                    yield stream_event

            # Append the complete message to result
            run_response.result.append(stream_response.message)

            iteration_data = IterationData(
                iterations=run_response.iterations,
                has_tool_calls=bool(stream_response.tool_calls)
            )

            logging.info(
                f"Iteration {run_response.iterations}: Received response - "
                f"content length: {len(stream_response.content)} chars, tool_calls: {len(stream_response.tool_calls)}"
            )

            if not stream_response.tool_calls:
                logging.info(f"No tool calls made. Agent finished after {run_response.iterations} iterations.")
                # Yield agent finished event
                yield RunnerStream.iteration_end(iteration_data)
                await self.tool.close_mcp_manager()
                run_response.sub_agents_response = self.tool.get_sub_agents_response()
                yield RunnerStream.agent_run_end(run_response)
                return

            tool_responses: List[ToolCallResponse] = await self.tool.tool_calls(
                stream_response.tool_calls, run_response.result
            )

            for tool_response in tool_responses:
                run_response.result.append(tool_response.model_dump())
                # Yield tool response event
                yield RunnerStream.tool_response(tool_response)

            # Yield iteration end event
            yield RunnerStream.iteration_end(iteration_data)

        # Yield agent finished event if max iterations reached
        await self.tool.close_mcp_manager()
        run_response.sub_agents_response = self.tool.get_sub_agents_response()
        run_response.max_iterations_reached = True
        yield RunnerStream.agent_run_end(run_response)

    async def run_stream(self, input: List[Dict[str, Any]] = [{}]):
        try:
            self.tool = await Tool(self).init_tools(self.agent)
            async for event in self.__stream_with_events(input):
                yield event
        except Exception as e:
            if self.tool:
                await self.tool.close_mcp_manager()
            raise

    def __parse_model_response(self, response) -> Dict:
        final_response = {"role": "assistant", "content": "", "tool_calls": []}

        if hasattr(response, "choices") and len(response.choices) > 0:
            res = response.choices[0].message

            if hasattr(res, "content") and res.content:
                final_response["content"] += res.content

            if hasattr(res, "tool_calls") and res.tool_calls:
                for tool_call in res.tool_calls:
                    final_response["tool_calls"].append({
                        "id": tool_call.id,
                        "type": "function",
                        "function": {
                            "name": tool_call.function.name
                            if hasattr(tool_call.function, "name")
                            else "",
                            "arguments": tool_call.function.arguments
                            if hasattr(tool_call.function, "arguments")
                            else "",
                        },
                    })
        if not final_response["content"]:
            del final_response["content"]
        
        if not final_response["tool_calls"]:
            del final_response["tool_calls"]

        return final_response

    async def __execute(self, input: List[Dict[str, Any]]):
        input = self.__set_agent_instructions(input, self.agent.get_instructions())
        run_response = RunResponse(result=input, iterations=0)
        # If sub agent, get the last index value of the input. It will be the system prompt in any way.
        if self.is_sub_agent and self.agent.get_instructions():
            run_response.sub_agent_result.append(input[-1])

        while run_response.iterations < self.agent.get_max_iterations():
            run_response.iterations += 1
            response = Model(self.agent.get_llm_provider()).run(
                model=self.agent.get_model(),
                messages=run_response.result,
                tools=self.tool.get_tools(),
                parallel_tool_calls = self.agent.get_parallel_tool_calls(),
                custom_llm_provider=self.agent.get_custom_llm_provider(),
                trace_id=self.agent.get_trace_id()
            )

            response = self.__parse_model_response(response)
            
            run_response.result.append(response)

            if self.is_sub_agent:
                run_response.sub_agent_result.append(response)

            if not response.get("tool_calls", []):
                run_response.sub_agents_response = self.tool.get_sub_agents_response()
                return run_response

            tool_responses: List[ToolCallResponse] = await self.tool.tool_calls(
                response.get("tool_calls"), run_response.result
            )

            for tool_response in tool_responses:
                run_response.result.append(tool_response.model_dump())

        run_response.sub_agents_response = self.tool.get_sub_agents_response()
        run_response.max_iterations_reached = True
        return run_response

    async def run_async(self, input: List[Dict[str, Any]]):
        try:
            # If caller function is 'run_sub_agent', its a sub agent call
            if (sys._getframe(1).f_code.co_name) == 'run_sub_agent':
                self.is_sub_agent = True
            self.tool = await Tool(self).init_tools(self.agent)
            exec_result = await self.__execute(input)
            await self.tool.close_mcp_manager()
            return exec_result
        except Exception as e:
            if self.tool:
                await self.tool.close_mcp_manager()
            raise

    def run(self, input: List[Dict[str, Any]] = [{}]):
        try:
            return asyncio.run(self.run_async(input))
        except Exception as e:
            raise

    @classmethod
    async def run_sub_agent(cls, agent: Agent, input=[{}]):
        return await cls(agent).run_async(input=input)
