"""OpenAI-compatible chat / embeddings / responses endpoints."""

import copy
import json

from flask import Blueprint, Response, request

from ._common import mock_tool_arguments, sse

bp = Blueprint("openai", __name__)


CHAT_RESPONSE = {
    "id": "chatcmpl-mock-001",
    "object": "chat.completion",
    "created": 1700000000,
    "model": "gpt-4o-mini",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "This is a response from the mock server.",
            },
            "finish_reason": "stop",
        }
    ],
    "usage": {
        "prompt_tokens": 25,
        "completion_tokens": 12,
        "total_tokens": 37,
    },
}

CHAT_TOOL_CALL_RESPONSE = {
    "id": "chatcmpl-mock-002",
    "object": "chat.completion",
    "created": 1700000000,
    "model": "gpt-4o-mini",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_mock_001",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"location": "Seattle"}',
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": {
        "prompt_tokens": 50,
        "completion_tokens": 20,
        "total_tokens": 70,
    },
}

EMBEDDING_RESPONSE = {
    "id": "embd-mock-001",
    "object": "list",
    "data": [
        {
            "object": "embedding",
            "index": 0,
            "embedding": [0.001] * 256,
        }
    ],
    "model": "text-embedding-3-small",
    "usage": {
        "prompt_tokens": 8,
        "total_tokens": 8,
    },
}

RESPONSES_RESPONSE = {
    "id": "resp-mock-001",
    "object": "response",
    "created_at": 1700000000,
    "model": "gpt-4o-mini",
    "output": [
        {
            "type": "message",
            "id": "msg-mock-001",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": "This is a response from the mock server.",
                }
            ],
        }
    ],
    "usage": {
        "input_tokens": 25,
        "output_tokens": 12,
        "total_tokens": 37,
    },
}


def _mock_chat_content(body):
    response_format = body.get("response_format") or {}
    if response_format.get("type") != "json_object":
        return "This is a response from the mock server."

    message_text = "\n".join(
        message.get("content", "") for message in body.get("messages", []) if isinstance(message.get("content"), str)
    )
    if "Relevance-Judge" in message_text or "Relevance Evaluator" in message_text:
        return json.dumps(
            {
                "explanation": "The response directly answers the user's question and stays fully on topic.",
                "score": 5,
            }
        )

    return json.dumps(
        {
            "explanation": "The response satisfies the evaluator request.",
            "score": 5,
        }
    )


def _stream_chat(body):
    """Yield SSE chunks for an OpenAI streaming chat completion."""
    model = body.get("model", "gpt-4o-mini")
    chunk_id = "chatcmpl-mock-stream-001"

    # role chunk
    yield sse(
        {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
        }
    )

    # content chunks
    for word in ["This ", "is ", "a ", "mock ", "streamed ", "response."]:
        yield sse(
            {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": model,
                "choices": [{"index": 0, "delta": {"content": word}, "finish_reason": None}],
            }
        )

    # usage chunk
    yield sse(
        {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 25,
                "completion_tokens": 6,
                "total_tokens": 31,
            },
        }
    )

    yield "data: [DONE]\n\n"


@bp.route("/v1/chat/completions", methods=["POST"])
@bp.route("/openai/v1/chat/completions", methods=["POST"])
@bp.route("/openai/deployments/<deployment>/chat/completions", methods=["POST"])
@bp.route("/chat/completions", methods=["POST"])
def chat_completions(deployment=None):
    body = request.get_json(silent=True) or {}

    # Streaming
    if body.get("stream"):
        return Response(_stream_chat(body), mimetype="text/event-stream")

    # Tool-call detection: if tools are provided and no tool result yet,
    # return a tool call; otherwise return a normal response (completes the
    # agent loop).
    if body.get("tools"):
        messages = body.get("messages", [])
        has_tool_result = any(m.get("role") == "tool" for m in messages)
        if not has_tool_result:
            resp = copy.deepcopy(CHAT_TOOL_CALL_RESPONSE)
            resp["model"] = body.get("model", resp["model"])
            tool = body.get("tools", [{}])[0]
            tool_name = tool.get("function", {}).get("name")
            if tool_name:
                resp["choices"][0]["message"]["tool_calls"][0]["function"]["name"] = tool_name
            resp["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = json.dumps(
                mock_tool_arguments(tool)
            )
            return resp

    resp = dict(CHAT_RESPONSE)
    resp["model"] = body.get("model", resp["model"])
    resp["choices"] = copy.deepcopy(resp["choices"])
    resp["choices"][0]["message"]["content"] = _mock_chat_content(body)
    return resp


@bp.route("/v1/embeddings", methods=["POST"])
@bp.route("/openai/v1/embeddings", methods=["POST"])
@bp.route("/openai/deployments/<deployment>/embeddings", methods=["POST"])
@bp.route("/embeddings", methods=["POST"])
def embeddings(deployment=None):
    body = request.get_json(silent=True) or {}
    resp = dict(EMBEDDING_RESPONSE)
    resp["model"] = body.get("model", resp["model"])
    return resp


@bp.route("/v1/responses", methods=["POST"])
@bp.route("/openai/v1/responses", methods=["POST"])
def responses():
    body = request.get_json(silent=True) or {}
    resp = dict(RESPONSES_RESPONSE)
    resp["model"] = body.get("model", resp["model"])
    return resp
