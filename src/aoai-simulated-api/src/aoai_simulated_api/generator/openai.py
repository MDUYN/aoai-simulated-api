import asyncio
import json
import logging
import time
import random

import lorem
import nanoid

from fastapi import Response
from fastapi.responses import StreamingResponse

from aoai_simulated_api.models import RequestContext
from aoai_simulated_api.constants import (
    SIMULATOR_KEY_DEPLOYMENT_NAME,
    SIMULATOR_KEY_OPENAI_COMPLETION_TOKENS,
    SIMULATOR_KEY_OPENAI_TOTAL_TOKENS,
    SIMULATOR_KEY_LIMITER,
    SIMULATOR_KEY_OPERATION_NAME,
)
from aoai_simulated_api.generator.openai_tokens import num_tokens_from_string, num_tokens_from_messages

# This file contains a default implementation of the openai generators
# You can configure your own generators by creating a generator_config.py file and setting the
# EXTENSION_PATH environment variable to the path of the file when running the API
# See src/examples/generator_config for an example of how to define your own generators

logger = logging.getLogger(__name__)

# 0.72 is based on generating a bunch of lorem ipsum and counting the tokens
# This was for a gpt-3.5 model
TOKEN_TO_WORD_FACTOR = 0.72

# API docs: https://learn.microsoft.com/en-gb/azure/ai-services/openai/reference

missing_deployment_names = set()

# pylint: disable-next=invalid-name
default_embedding_size = (
    1536  # text-embedding-3-small default (https://platform.openai.com/docs/guides/embeddings/what-are-embeddings)
)


def get_model_name_from_deployment_name(context: RequestContext, deployment_name: str) -> str:
    deployments = context.config.openai_deployments
    if deployments:
        deployment = deployments.get(deployment_name)
        if deployment:
            return deployment.model

    default_model = "gpt-3.5-turbo-0613"

    # Output warning for missing deployment name (only the first time we encounter it)
    if deployment_name not in missing_deployment_names:
        missing_deployment_names.add(deployment_name)
        logger.warning("Deployment %s not found in config, using default model %s", deployment_name, default_model)
    return default_model


def _generate_embedding(index: int, embedding_size=default_embedding_size):
    """Generates a random embedding"""
    return {
        "object": "embedding",
        "index": index,
        "embedding": [(random.random() - 0.5) * 4 for _ in range(embedding_size)],
    }


async def azure_openai_embedding(context: RequestContext) -> Response | None:
    request = context.request
    is_match, path_params = context.is_route_match(
        request=request, path="/openai/deployments/{deployment}/embeddings", methods=["POST"]
    )
    if not is_match:
        return None

    deployment_name = path_params["deployment"]
    request_body = await request.json()
    model_name = get_model_name_from_deployment_name(context, deployment_name)
    request_input = request_body["input"]
    embeddings = []
    if isinstance(request_input, str):
        tokens = num_tokens_from_string(request_input, model_name)
        embeddings.append(_generate_embedding(0))
    else:
        tokens = 0
        index = 0
        for i in request_input:
            tokens += num_tokens_from_string(i, model_name)
            embeddings.append(_generate_embedding(index))
            index += 1

    response_data = {
        "object": "list",
        "data": embeddings,
        "model": "ada",
        "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
    }

    # store values in the context for use by the rate-limiter etc
    context.values[SIMULATOR_KEY_LIMITER] = "openai"
    context.values[SIMULATOR_KEY_OPERATION_NAME] = "embeddings"
    context.values[SIMULATOR_KEY_DEPLOYMENT_NAME] = deployment_name
    context.values[SIMULATOR_KEY_OPENAI_TOTAL_TOKENS] = tokens

    return Response(
        status_code=200,
        content=json.dumps(response_data),
        headers={
            "Content-Type": "application/json",
        },
    )


async def azure_openai_completion(context: RequestContext) -> Response | None:
    request = context.request
    is_match, path_params = context.is_route_match(
        request=request, path="/openai/deployments/{deployment}/completions", methods=["POST"]
    )
    if not is_match:
        return None

    deployment_name = path_params["deployment"]
    model_name = get_model_name_from_deployment_name(context, deployment_name)
    request_body = await request.json()
    prompt_tokens = num_tokens_from_string(request_body["prompt"], model_name)

    # TODO - determine the maxiumum tokens to use based on the model
    max_tokens = request_body.get("max_tokens", 4096)

    # TODO - randomise the finish reason (i.e. don't always use the full set of tokens)
    words_to_generate = int(TOKEN_TO_WORD_FACTOR * max_tokens)
    text = "".join(lorem.get_word(count=words_to_generate))

    completion_tokens = num_tokens_from_string(text, model_name)
    total_tokens = prompt_tokens + completion_tokens

    response_body = {
        "id": "cmpl-" + nanoid.non_secure_generate(size=29),
        "object": "text_completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [
            {
                "text": text,
                "index": 0,
                "finish_reason": "length",
                "logprobs": None,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
    }

    # store values in the context for use by the rate-limiter etc
    context.values[SIMULATOR_KEY_LIMITER] = "openai"
    context.values[SIMULATOR_KEY_OPERATION_NAME] = "completions"
    context.values[SIMULATOR_KEY_DEPLOYMENT_NAME] = deployment_name
    context.values[SIMULATOR_KEY_OPENAI_COMPLETION_TOKENS] = completion_tokens
    context.values[SIMULATOR_KEY_OPENAI_TOTAL_TOKENS] = total_tokens

    return Response(
        content=json.dumps(response_body),
        headers={
            "Content-Type": "application/json",
        },
        status_code=200,
    )


async def azure_openai_chat_completion(context: RequestContext) -> Response | None:
    request = context.request
    is_match, path_params = context.is_route_match(
        request=request, path="/openai/deployments/{deployment}/chat/completions", methods=["POST"]
    )
    if not is_match:
        return None

    request_body = await request.json()
    deployment_name = path_params["deployment"]
    model_name = get_model_name_from_deployment_name(context, deployment_name)
    prompt_tokens = num_tokens_from_messages(request_body["messages"], model_name)

    # TODO - determine the maxiumum tokens to use based on the model
    max_tokens = request_body.get("max_tokens", 4096)
    # TODO - randomise the finish reason (i.e. don't always use the full set of tokens)
    words_to_generate = int(TOKEN_TO_WORD_FACTOR * max_tokens)
    words = lorem.get_word(count=words_to_generate)

    if request_body.get("stream", False):

        async def send_words():
            space = ""
            for word in words.split(" "):
                chunk_string = json.dumps(
                    {
                        "id": "chatcmpl-" + nanoid.non_secure_generate(size=29),
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model_name": model_name,
                        "system_fingerprint": None,
                        "choices": [
                            {
                                "delta": {
                                    "content": space + word,
                                    "function_call": None,
                                    "role": None,
                                    "tool_calls": None,
                                    "finish_reason": None,
                                    "index": 0,
                                    "logprobs": None,
                                    "content_filter_results": {
                                        "hate": {"filtered": False, "severity": "safe"},
                                        "self_harm": {"filtered": False, "severity": "safe"},
                                        "sexual": {"filtered": False, "severity": "safe"},
                                        "violence": {"filtered": False, "severity": "safe"},
                                    },
                                },
                                "message": {"role": "assistant", "content": word},
                            },
                        ],
                    }
                )

                yield "data: " + chunk_string + "\n"
                yield "\n"
                await asyncio.sleep(0.05)
                space = " "
            yield "[DONE]"

        return StreamingResponse(content=send_words())

    text = "".join(words)
    completion_tokens = num_tokens_from_string(text, model_name)
    total_tokens = prompt_tokens + completion_tokens

    response_body = {
        "id": "chatcmpl-" + nanoid.non_secure_generate(size=29),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "prompt_filter_results": [
            {
                "prompt_index": 0,
                "content_filter_results": {
                    "hate": {"filtered": False, "severity": "safe"},
                    "self_harm": {"filtered": False, "severity": "safe"},
                    "sexual": {"filtered": False, "severity": "safe"},
                    "violence": {"filtered": False, "severity": "safe"},
                },
            }
        ],
        "choices": [
            {
                "finish_reason": "length",
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": text,
                },
                "content_filter_results": {
                    "hate": {"filtered": False, "severity": "safe"},
                    "self_harm": {"filtered": False, "severity": "safe"},
                    "sexual": {"filtered": False, "severity": "safe"},
                    "violence": {"filtered": False, "severity": "safe"},
                },
            },
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
    }

    # store values in the context for use by the rate-limiter etc
    context.values[SIMULATOR_KEY_LIMITER] = "openai"
    context.values[SIMULATOR_KEY_OPERATION_NAME] = "chat-completions"
    context.values[SIMULATOR_KEY_DEPLOYMENT_NAME] = deployment_name
    context.values[SIMULATOR_KEY_OPENAI_COMPLETION_TOKENS] = completion_tokens
    context.values[SIMULATOR_KEY_OPENAI_TOTAL_TOKENS] = total_tokens

    return Response(
        content=json.dumps(response_body),
        headers={
            "Content-Type": "application/json",
        },
        status_code=200,
    )
