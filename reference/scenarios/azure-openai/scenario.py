"""Reference implementation for Azure OpenAI."""

import os

from reference_shared import flush_and_shutdown, mock_server_host_port, reference_tracer, setup_otel

MOCK_BASE_URL = os.environ["MOCK_LLM_URL"]

_reference_tracer = reference_tracer()


def run_chat_reference(client):
    """Scenario: basic chat completion with reference implementation."""
    print("  [chat] basic chat completion (reference implementation)")
    request_model = "gpt-4o-mini"
    with _reference_tracer.start_as_current_span("chat gpt-4o-mini") as span:
        host, port = mock_server_host_port(MOCK_BASE_URL)
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.provider.name", "azure.openai")
        span.set_attribute("gen_ai.request.model", request_model)
        if host:
            span.set_attribute("server.address", host)
        if port is not None:
            span.set_attribute("server.port", port)
        messages = [{"role": "user", "content": "Say hello."}]
        resp = client.chat.completions.create(
            model=request_model,
            messages=messages,
        )
        span.set_attribute("gen_ai.response.model", resp.model)
        span.set_attribute("gen_ai.response.id", resp.id)
        span.set_attribute("gen_ai.response.finish_reasons", [c.finish_reason for c in resp.choices])
        if resp.usage:
            span.set_attribute("gen_ai.usage.input_tokens", resp.usage.prompt_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", resp.usage.completion_tokens)
        print(f"    -> {resp.choices[0].message.content[:60]}")


def run_embeddings_reference(client):
    """Scenario: embedding generation with reference implementation."""
    print("  [embeddings] embedding generation (reference implementation)")
    request_model = "text-embedding-3-small"
    request_encoding_format = "float"
    with _reference_tracer.start_as_current_span("embeddings text-embedding-3-small") as span:
        host, port = mock_server_host_port(MOCK_BASE_URL)
        span.set_attribute("gen_ai.operation.name", "embeddings")
        span.set_attribute("gen_ai.provider.name", "azure.openai")
        span.set_attribute("gen_ai.request.model", request_model)
        span.set_attribute("gen_ai.request.encoding_formats", [request_encoding_format])
        if host:
            span.set_attribute("server.address", host)
        if port is not None:
            span.set_attribute("server.port", port)
        resp = client.embeddings.create(
            model=request_model,
            input="Hello, world!",
            encoding_format=request_encoding_format,
        )
        span.set_attribute("gen_ai.response.model", resp.model)
        if resp.data and resp.data[0].embedding is not None:
            span.set_attribute("gen_ai.embeddings.dimension.count", len(resp.data[0].embedding))
        if resp.usage:
            span.set_attribute("gen_ai.usage.input_tokens", resp.usage.prompt_tokens)
        print(f"    -> embedding dim: {len(resp.data[0].embedding)}")


def main():
    print("=== Reference Implementation: Azure OpenAI Reference ===")

    tp, lp, mp = setup_otel()

    import openai

    client = openai.AzureOpenAI(
        azure_endpoint=MOCK_BASE_URL,
        api_key="mock-key",
        api_version="2024-06-01",
    )

    run_chat_reference(client)
    run_embeddings_reference(client)

    flush_and_shutdown(tp, lp, mp)


if __name__ == "__main__":
    main()
