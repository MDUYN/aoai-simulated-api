import asyncio
import json
import logging
import time
import random

import lorem
import nanoid

from fastapi import Response
from fastapi.responses import StreamingResponse

from aoai_simulated_api import constants
from aoai_simulated_api.auth import validate_api_key_header
from aoai_simulated_api.models import RequestContext, OpenAIDeployment
from aoai_simulated_api.constants import (
    SIMULATOR_KEY_DEPLOYMENT_NAME,
    SIMULATOR_KEY_OPENAI_COMPLETION_TOKENS,
    SIMULATOR_KEY_OPENAI_TOTAL_TOKENS,
    SIMULATOR_KEY_LIMITER,
    SIMULATOR_KEY_OPERATION_NAME,
)
from aoai_simulated_api.generator.openai_tokens import (
    get_max_completion_tokens,
    num_tokens_from_string,
    num_tokens_from_messages,
)

# This file contains a default implementation of the openai generators
# You can configure your own generators by creating a generator_config.py file and setting the
# EXTENSION_PATH environment variable to the path of the file when running the API
# See src/examples/generator_echo for an example of how to define your own generators

logger = logging.getLogger(__name__)

# API docs: https://learn.microsoft.com/en-gb/azure/ai-services/openai/reference

missing_deployment_names = set()
missing_embedding_deployment_names = set()
default_openai_embedding_model = OpenAIDeployment(
    name="embedding",
    model="text-embedding-ada-002",
    tokens_per_minute=10000,
    embedding_size=1536
)

def get_embedding_model_from_deployment_name(
    context: RequestContext, deployment_name: str
) -> OpenAIDeployment | None:
    """
    Gets the model name for the specified embedding deployment. If
    the deployment is not in the configured deployments then the
    default model is returned and a warning is logged.

    Args:
        context: RequestContext instance
        deployment_name: Name of the deployment

    Returns:
        OpenAIDeployment | None: Instance of OpenAIDeployment
    """
    deployments = context.config.openai_deployments

    if deployments:
        deployment = deployments.get(deployment_name)

        if deployment:
            return deployment

    if context.config.allow_undefined_openai_deployments:
        default_model_name = "embedding"

        # Output warning for missing embedding deployment name (only the
        # first time we encounter it)
        if deployment_name not in missing_embedding_deployment_names:
            missing_embedding_deployment_names.add(default_model_name)
            logger.warning(
                "Deployment %s not found in config and "
                "allow_undefined_openai_deployments is True. "
                "Using default model %s",
                deployment_name,
                default_model_name,
            )
        return default_openai_embedding_model
    else:
        # Output warning for missing embedding deployment name
        # (only the first time we encounter it)
        if deployment_name not in missing_deployment_names:
            missing_deployment_names.add(deployment_name)
            logger.warning(
                "Deployment %s not found in config and "
                "allow_undefined_openai_deployments is False", deployment_name
            )
        return None


def get_model_name_from_deployment_name(context: RequestContext, deployment_name: str) -> str | None:
    """
    Gets the model name for the specified deployment.
    If the deployment is not in the configured deployments then either a default model is returned (if )
    """
    deployments = context.config.openai_deployments
    if deployments:
        deployment = deployments.get(deployment_name)
        if deployment:
            return deployment.model

    if context.config.allow_undefined_openai_deployments:
        default_model = "gpt-3.5-turbo-0613"

        # Output warning for missing deployment name (only the first time we encounter it)
        if deployment_name not in missing_deployment_names:
            missing_deployment_names.add(deployment_name)
            logger.warning(
                "Deployment %s not found in config and allow_undefined_openai_deployments is True. Using default model %s",
                deployment_name,
                default_model,
            )
        return default_model
    else:
        # Output warning for missing deployment name (only the first time we encounter it)
        if deployment_name not in missing_deployment_names:
            missing_deployment_names.add(deployment_name)
            logger.warning(
                "Deployment %s not found in config and allow_undefined_openai_deployments is False", deployment_name
            )
        return None


async def calculate_latency(context: RequestContext, status_code: int):
    """Calculate additional latency that should be applied"""
    if status_code >= 300:
        return

    # Determine the target latency for the request
    completion_tokens = context.values.get(constants.SIMULATOR_KEY_OPENAI_COMPLETION_TOKENS)

    if completion_tokens and completion_tokens > 0:
        config = context.config
        operation_name = context.values.get(constants.SIMULATOR_KEY_OPERATION_NAME)
        target_duration_ms = None
        if operation_name == "embeddings":
            # embeddings config returns latency value to use (in milliseconds)
            target_duration_ms = config.latency.open_ai_embeddings.get_value()
        elif operation_name == "completions":
            # completions config returns latency per completion token in milliseconds
            target_duration_ms = config.latency.open_ai_completions.get_value()
        elif operation_name == "chat-completions":
            # chat completions config returns latency per completion token in milliseconds
            target_duration_ms = config.latency.open_ai_chat_completions.get_value() * completion_tokens

        if target_duration_ms:
            context.values[constants.TARGET_DURATION_MS] = target_duration_ms


def create_embedding_content(index: int, embedding_size):
    """Generates a random embedding"""
    return {
        "object": "embedding",
        "index": index,
        "embedding": [(random.random() - 0.5) * 4 for _ in range(embedding_size)],
    }


def create_embeddings_response(
    context: RequestContext,
    deployment_name: str,
    model: OpenAIDeployment,
    request_input: str | list,
):
    embeddings = []
    if isinstance(request_input, str):
        tokens = num_tokens_from_string(request_input, model.name)
        embeddings.append(
            create_embedding_content(0, embedding_size=model.embedding_size)
        )
    else:
        tokens = 0
        index = 0
        for i in request_input:
            tokens += num_tokens_from_string(i, model.name)
            embeddings.append(
                create_embedding_content(
                    index, embedding_size=model.embedding_size
                )
            )
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


def generate_lorem_text(max_tokens: int, model_name: str):
    # The simplest approach to generating the compltion would
    # be to add a word at a time and count the tokens until we reach the limit
    # For large max_token values that will be slow, so we
    # estimate the number of words to generate based on the max_tokens
    # opting to stay below the limit (based on experimentation)
    # and then top up
    init_word_count = int(0.5 * max_tokens)
    text = lorem.get_word(count=init_word_count)
    while True:
        new_text = text + " " + lorem.get_word()
        if num_tokens_from_string(new_text, model_name) > max_tokens:
            break
        text = new_text
    return text


def create_completion_response(
    context: RequestContext,
    deployment_name: str,
    model_name: str,
    prompt_tokens: int,
    max_tokens: int,
):
    """
    Creates a Response object for a completion request and sets context values for the rate-limiter etc
    """
    text = generate_lorem_text(max_tokens=max_tokens, model_name=model_name)

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


def create_lorem_chat_completion_response(
    context: RequestContext,
    deployment_name: str,
    model_name: str,
    streaming: bool,
    max_tokens: int,
    prompt_messages: list,
    finish_reason: str = "length",
):
    """
    Creates a Response object for a chat completion request by generating
    lorem ipsum text and sets context values for the rate-limiter etc.
    Handles streaming vs non-streaming
    """

    text = generate_lorem_text(max_tokens=max_tokens, model_name=model_name)

    return create_chat_completion_response(
        context=context,
        deployment_name=deployment_name,
        model_name=model_name,
        streaming=streaming,
        prompt_messages=prompt_messages,
        generated_content=text,
        finish_reason=finish_reason,
    )


def create_chat_completion_response(
    context: RequestContext,
    deployment_name: str,
    model_name: str,
    streaming: bool,
    prompt_messages: list,
    generated_content: str,
    finish_reason: str = "length",
):
    """
    Creates a Response object for a chat completion request and sets context values for the rate-limiter etc.
    Handles streaming vs non-streaming
    """

    prompt_tokens = num_tokens_from_messages(prompt_messages, model_name)

    if streaming:

        async def send_words():
            space = ""
            role = "assistant"
            for word in generated_content.split(" "):
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
                                    "role": role,
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
                            },
                        ],
                    }
                )
                role = None

                yield "data: " + chunk_string + "\n"
                yield "\n"
                await asyncio.sleep(0.05)
                space = " "

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
                                "content": None,
                                "function_call": None,
                                "role": None,
                                "tool_calls": None,
                                "finish_reason": finish_reason,
                                "index": 0,
                                "logprobs": None,
                                "content_filter_results": {
                                    "hate": {"filtered": False, "severity": "safe"},
                                    "self_harm": {"filtered": False, "severity": "safe"},
                                    "sexual": {"filtered": False, "severity": "safe"},
                                    "violence": {"filtered": False, "severity": "safe"},
                                },
                            },
                        },
                    ],
                }
            )

            yield "data: " + chunk_string + "\n"
            yield "\n"
            yield "[DONE]"

        return StreamingResponse(content=send_words())

    text = "".join(generated_content)
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
                "finish_reason": finish_reason,
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


def _validate_api_key_header(context: RequestContext):
    request = context.request
    validate_api_key_header(request=request, header_name="api-key", allowed_key_value=context.config.simulator_api_key)


async def azure_openai_embedding(context: RequestContext) -> Response | None:
    request = context.request
    is_match, path_params = context.is_route_match(
        request=request, path="/openai/deployments/{deployment}/embeddings", methods=["POST"]
    )
    if not is_match:
        return None

    _validate_api_key_header(context)
    deployment_name = path_params["deployment"]
    request_body = await request.json()
    model = get_embedding_model_from_deployment_name(
        context, deployment_name
    )

    if model is None:
        return Response(
            status_code=404,
            content=json.dumps({"error": f"Deployment {deployment_name} not found"}),
            headers={
                "Content-Type": "application/json",
            },
        )
    request_input = request_body["input"]

    # calculate a simulated latency and store in context.values
    await calculate_latency(context, 200)

    return create_embeddings_response(
        context=context,
        deployment_name=deployment_name,
        model=model,
        request_input=request_input,
    )


async def azure_openai_completion(context: RequestContext) -> Response | None:
    request = context.request
    is_match, path_params = context.is_route_match(
        request=request, path="/openai/deployments/{deployment}/completions", methods=["POST"]
    )
    if not is_match:
        return None

    _validate_api_key_header(context)

    deployment_name = path_params["deployment"]
    model_name = get_model_name_from_deployment_name(context, deployment_name)
    if model_name is None:
        return Response(
            status_code=404,
            content=json.dumps({"error": f"Deployment {deployment_name} not found"}),
            headers={
                "Content-Type": "application/json",
            },
        )
    request_body = await request.json()
    prompt_tokens = num_tokens_from_string(request_body["prompt"], model_name)

    max_tokens = get_max_completion_tokens(request_body, model_name, prompt_tokens=prompt_tokens)

    # calculate a simulated latency and store in context.values
    await calculate_latency(context, 200)

    return create_completion_response(
        context=context,
        deployment_name=deployment_name,
        model_name=model_name,
        prompt_tokens=prompt_tokens,
        max_tokens=max_tokens,
    )


async def azure_openai_chat_completion(context: RequestContext) -> Response | None:
    request = context.request
    is_match, path_params = context.is_route_match(
        request=request, path="/openai/deployments/{deployment}/chat/completions", methods=["POST"]
    )
    if not is_match:
        return None

    _validate_api_key_header(context)

    request_body = await request.json()
    deployment_name = path_params["deployment"]
    model_name = get_model_name_from_deployment_name(context, deployment_name)
    if model_name is None:
        return Response(
            status_code=404,
            content=json.dumps({"error": f"Deployment {deployment_name} not found"}),
            headers={
                "Content-Type": "application/json",
            },
        )
    messages = request_body["messages"]
    prompt_tokens = num_tokens_from_messages(messages, model_name)

    max_tokens = get_max_completion_tokens(request_body, model_name, prompt_tokens=prompt_tokens)

    streaming = request_body.get("stream", False)

    # calculate a simulated latency and store in context.values
    await calculate_latency(context, 200)

    return create_lorem_chat_completion_response(
        context=context,
        deployment_name=deployment_name,
        model_name=model_name,
        streaming=streaming,
        max_tokens=max_tokens,
        prompt_messages=messages,
    )
