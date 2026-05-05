"""Reference implementation for CrewAI.

Exercises: agent task execution, agent planning (CrewPlanner)
against a mock OpenAI server, with manual OTel spans.
"""

import json
import os

from reference_shared import flush_and_shutdown, mock_server_host_port, reference_tracer, setup_otel

MOCK_BASE_URL = os.environ["MOCK_LLM_URL"] + "/v1"

_reference_tracer = reference_tracer()


def run_crew():
    """Scenario: basic crew task execution with reference implementation."""
    print("  [crew] basic crew task execution (reference implementation)")
    os.environ["CREWAI_DISABLE_TELEMETRY"] = "true"
    os.environ["CREWAI_DISABLE_TRACKING"] = "true"
    os.environ["CREWAI_TRACING_ENABLED"] = "false"
    from crewai import LLM, Agent, Crew, Task
    from crewai.tools import tool

    request_model = "gpt-4o-mini"
    request_choice_count = 2
    request_temperature = 0.2
    request_top_p = 0.9
    request_max_tokens = 64
    request_seed = 7
    request_stop_sequences = ["<END>"]
    request_frequency_penalty = 0.1
    request_presence_penalty = 0.2
    system_prompt = "You are a helpful research assistant."
    host, port = mock_server_host_port(MOCK_BASE_URL)
    os.environ["OPENAI_API_KEY"] = "mock-key"
    os.environ["OPENAI_API_BASE"] = MOCK_BASE_URL
    os.environ["OPENAI_MODEL_NAME"] = request_model
    llm = LLM(
        model=request_model,
        provider="openai",
        base_url=MOCK_BASE_URL,
        api_key="mock-key",
        temperature=request_temperature,
        top_p=request_top_p,
        n=request_choice_count,
        max_completion_tokens=request_max_tokens,
        seed=request_seed,
        stop=request_stop_sequences,
        frequency_penalty=request_frequency_penalty,
        presence_penalty=request_presence_penalty,
    )
    captured_completion = None

    @tool
    def get_weather(location: str) -> str:
        """Get the current weather for a location."""
        with _reference_tracer.start_as_current_span("execute_tool get_weather") as tool_span:
            tool_span.set_attribute("gen_ai.operation.name", "execute_tool")
            tool_span.set_attribute("gen_ai.tool.name", "get_weather")
            tool_span.set_attribute("gen_ai.tool.description", get_weather.func.__doc__ or "")
            tool_span.set_attribute("gen_ai.tool.type", "function")
            tool_span.set_attribute(
                "gen_ai.tool.call.arguments",
                json.dumps({"location": location}),
            )
            result = "Sunny, 72°F"
            tool_span.set_attribute("gen_ai.tool.call.result", result)
            return result

    tools = [get_weather]

    researcher_role = "Researcher"
    with _reference_tracer.start_as_current_span("create_agent Researcher") as create_agent_span:
        create_agent_span.set_attribute("gen_ai.operation.name", "create_agent")
        create_agent_span.set_attribute("gen_ai.provider.name", "openai")
        create_agent_span.set_attribute("gen_ai.request.model", request_model)
        if host:
            create_agent_span.set_attribute("server.address", host)
        if port is not None:
            create_agent_span.set_attribute("server.port", port)
        create_agent_span.set_attribute(
            "gen_ai.system_instructions",
            json.dumps([{"parts": [{"type": "text", "content": system_prompt}]}]),
        )
        researcher = Agent(
            role=researcher_role,
            goal="Find information",
            backstory=system_prompt,
            tools=tools,
            llm=llm,
            verbose=False,
            allow_delegation=False,
        )
        create_agent_span.set_attribute("gen_ai.agent.id", str(researcher.id))
        create_agent_span.set_attribute("gen_ai.agent.name", researcher.role)

    task = Task(
        description="Use the get_weather tool to report the weather in Seattle.",
        expected_output="The current weather.",
        agent=researcher,
    )

    crew = Crew(agents=[researcher], tasks=[task], verbose=False)
    workflow_name = getattr(crew, "name", None)

    with _reference_tracer.start_as_current_span("invoke_workflow crew") as workflow_span:
        workflow_span.set_attribute("gen_ai.operation.name", "invoke_workflow")
        if workflow_name:
            workflow_span.set_attribute("gen_ai.workflow.name", workflow_name)
        workflow_span.set_attribute(
            "gen_ai.input.messages",
            json.dumps(
                [
                    {"role": "user", "parts": [{"type": "text", "content": task.description}]},
                ]
            ),
        )

        with _reference_tracer.start_as_current_span("chat gpt-4o-mini") as span:
            span.set_attribute("gen_ai.operation.name", "chat")
            span.set_attribute("gen_ai.provider.name", "openai")
            span.set_attribute("gen_ai.request.model", request_model)
            span.set_attribute("gen_ai.request.choice.count", request_choice_count)
            span.set_attribute("gen_ai.request.max_tokens", request_max_tokens)
            span.set_attribute("gen_ai.request.temperature", request_temperature)
            span.set_attribute("gen_ai.request.seed", request_seed)
            span.set_attribute("gen_ai.request.stop_sequences", request_stop_sequences)
            span.set_attribute("gen_ai.request.frequency_penalty", request_frequency_penalty)
            span.set_attribute("gen_ai.request.presence_penalty", request_presence_penalty)
            span.set_attribute("gen_ai.request.top_p", request_top_p)
            span.set_attribute(
                "gen_ai.system_instructions",
                json.dumps([{"parts": [{"type": "text", "content": system_prompt}]}]),
            )
            span.set_attribute(
                "gen_ai.input.messages",
                json.dumps(
                    [
                        {"role": "user", "parts": [{"type": "text", "content": task.description}]},
                    ]
                ),
            )
            # CrewAI converts tools to OpenAI function-calling format before
            # passing them to litellm, so we mirror that shape here.
            span.set_attribute(
                "gen_ai.tool.definitions",
                json.dumps(
                    [
                        {
                            "type": "function",
                            "function": {
                                "name": t.name,
                                "description": t.func.__doc__,
                                "parameters": t.args_schema.model_json_schema(),
                            },
                        }
                        for t in tools
                    ]
                ),
            )
            if host:
                span.set_attribute("server.address", host)
            if port is not None:
                span.set_attribute("server.port", port)
            original_create = researcher.llm._client.chat.completions.create

            def _capture_completion(*args, **kwargs):
                nonlocal captured_completion
                response = original_create(*args, **kwargs)
                captured_completion = response
                return response

            researcher.llm._client.chat.completions.create = _capture_completion
            try:
                result = crew.kickoff()
            finally:
                researcher.llm._client.chat.completions.create = original_create
            if captured_completion is not None:
                span.set_attribute("gen_ai.response.model", captured_completion.model)
                span.set_attribute("gen_ai.response.id", captured_completion.id)
                span.set_attribute(
                    "gen_ai.response.finish_reasons",
                    [choice.finish_reason for choice in captured_completion.choices],
                )
                if captured_completion.usage:
                    span.set_attribute("gen_ai.usage.input_tokens", captured_completion.usage.prompt_tokens)
                    span.set_attribute("gen_ai.usage.output_tokens", captured_completion.usage.completion_tokens)
            span.set_attribute(
                "gen_ai.output.messages",
                json.dumps(
                    [
                        {
                            "role": "assistant",
                            "parts": [{"type": "text", "content": str(result)}],
                        }
                    ]
                ),
            )
            workflow_span.set_attribute(
                "gen_ai.output.messages",
                json.dumps(
                    [
                        {
                            "role": "assistant",
                            "parts": [{"type": "text", "content": str(result)}],
                        }
                    ]
                ),
            )
            print(f"    -> {str(result)[:60]}")


def run_crew_planning():
    """Scenario: agent planning phase via CrewPlanner with reference implementation.

    CrewPlanner produces a strategy before tasks are executed. CrewPlanner
    synthesizes its own internal Agent(role="Task Execution Planner", ...) to
    actually run the planning LLM call (see CrewPlanner._create_planning_agent
    in crewai/utilities/planning_handler.py); that planner agent -- not the
    worker agent owning the surrounding tasks -- is what `gen_ai.agent.id`
    and `gen_ai.agent.name` should identify on the plan span. The tool/task
    spans that follow from the produced plan are siblings under the
    surrounding invoke_agent span (not modeled here -- planning is emitted
    standalone).
    """
    print("  [crew] agent planning phase (reference implementation)")
    os.environ["CREWAI_DISABLE_TELEMETRY"] = "true"
    os.environ["CREWAI_DISABLE_TRACKING"] = "true"
    os.environ["CREWAI_TRACING_ENABLED"] = "false"
    from crewai import LLM, Agent, Task
    from crewai.utilities.planning_handler import CrewPlanner

    request_model = "gpt-4o-mini"
    system_prompt = "You are a helpful research assistant."
    host, port = mock_server_host_port(MOCK_BASE_URL)
    os.environ["OPENAI_API_KEY"] = "mock-key"
    os.environ["OPENAI_API_BASE"] = MOCK_BASE_URL
    os.environ["OPENAI_MODEL_NAME"] = request_model
    llm = LLM(
        model=request_model,
        provider="openai",
        base_url=MOCK_BASE_URL,
        api_key="mock-key",
    )

    researcher_role = "Researcher"
    researcher = Agent(
        role=researcher_role,
        goal="Find information",
        backstory=system_prompt,
        llm=llm,
        verbose=False,
        allow_delegation=False,
    )

    task = Task(
        description="Research the weather forecasting techniques used by meteorologists.",
        expected_output="A short summary of forecasting techniques.",
        agent=researcher,
    )

    # CrewPlanner._handle_crew_planning() drives a real LLM round-trip via
    # planning_agent.execute_task(...) with output_pydantic=PlannerTaskPydanticOutput,
    # which the mock server cannot satisfy without library-specific schema
    # support. The closest honest reference is therefore: build the same
    # planner agent CrewPlanner uses internally (Agent(role="Task Execution
    # Planner", ...)), drive _create_tasks_summary() to exercise the real
    # input-construction path, and emit the chat span manually around the
    # planning LLM call we cannot route through the mock here.
    planner = CrewPlanner(tasks=[task], planning_agent_llm=llm)
    tasks_summary = planner._create_tasks_summary()
    planning_agent = planner._create_planning_agent()

    with _reference_tracer.start_as_current_span(f"plan {planning_agent.role}") as plan_span:
        plan_span.set_attribute("gen_ai.operation.name", "plan")
        plan_span.set_attribute("gen_ai.agent.id", str(planning_agent.id))
        plan_span.set_attribute("gen_ai.agent.name", planning_agent.role)

        with _reference_tracer.start_as_current_span("chat gpt-4o-mini") as chat_span:
            chat_span.set_attribute("gen_ai.operation.name", "chat")
            chat_span.set_attribute("gen_ai.provider.name", "openai")
            chat_span.set_attribute("gen_ai.request.model", request_model)
            if host:
                chat_span.set_attribute("server.address", host)
            if port is not None:
                chat_span.set_attribute("server.port", port)
            chat_span.set_attribute(
                "gen_ai.input.messages",
                json.dumps(
                    [
                        {
                            "role": "user",
                            "parts": [{"type": "text", "content": tasks_summary}],
                        },
                    ]
                ),
            )
            print(f"    -> planned {len(planner.tasks)} task(s)")


def main():
    print("=== Reference Implementation: CrewAI Reference Implementation ===")

    tp, lp, mp = setup_otel()
    # NO instrument() call - reference implementation only

    run_crew()
    run_crew_planning()

    flush_and_shutdown(tp, lp, mp)


if __name__ == "__main__":
    main()
