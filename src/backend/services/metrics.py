import asyncio
import logging
import os
import time
import uuid
from functools import wraps
from typing import Any, Callable, Dict

from cohere.core.api_error import ApiError
from httpx import AsyncHTTPTransport
from httpx._client import AsyncClient
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from backend.chat.collate import to_dict
from backend.chat.enums import StreamEvent
from backend.schemas.cohere_chat import CohereChatRequest
from backend.schemas.metrics import MetricsData

REPORT_ENDPOINT = os.getenv("REPORT_ENDPOINT", None)
NUM_RETRIES = 0

import time

from starlette.responses import Response


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.trace_id = str(uuid.uuid4())
        request.state.agent = None
        request.state.user = None

        start_time = time.perf_counter()
        response = await call_next(request)
        duration_ms = time.perf_counter() - start_time

        data = self.get_data(request.scope, response, request, duration_ms)
        run_loop(data)
        return response

    def get_data(self, scope, response, request, duration_ms):
        data = {}

        if scope["type"] != "http":
            return None

        agent = self.get_agent(request)
        agent_id = agent.get("id", None) if agent else None

        data = MetricsData(
            method=self.get_method(scope),
            endpoint_name=self.get_endpoint_name(scope, request),
            user_id=self.get_user_id(request),
            user=self.get_user(request),
            success=self.get_success(response),
            trace_id=request.state.trace_id,
            status_code=self.get_status_code(response),
            object_ids=self.get_object_ids(request),
            assistant=agent,
            assistant_id=agent_id,
            duration_ms=duration_ms,
        )

        return data

    def get_method(self, scope):
        try:
            return scope["method"].lower()
        except KeyError:
            return "unknown"
        except Exception as e:
            logging.warning(f"Failed to get method:  {e}")
            return "unknown"

    def get_endpoint_name(self, scope, request):
        try:
            path = scope["path"]
            # Replace path parameters with their names
            for key, value in request.path_params.items():
                path = path.replace(value, f":{key}")

            path = path[:-1] if path.endswith("/") else path
            return path.lower()
        except KeyError:
            return "unknown"
        except Exception as e:
            logging.warning(f"Failed to get endpoint name: {e}")
            return "unknown"

    def get_status_code(self, response):
        try:
            return response.status_code
        except Exception as e:
            logging.warning(f"Failed to get status code: {e}")
            return 500

    def get_success(self, response):
        try:
            return 200 <= response.status_code < 400
        except Exception as e:
            logging.warning(f"Failed to get success: {e}")
            return False

    def get_user_id(self, request):
        try:
            user_id = request.headers.get("User-Id", None)

            if not user_id:
                user_id = (
                    request.state.user.id
                    if hasattr(request.state, "user") and request.state.user
                    else None
                )

            return user_id
        except Exception as e:
            logging.warning(f"Failed to get user id: {e}")
            return None

    def get_user(self, request):
        if not hasattr(request.state, "user") or not request.state.user:
            return None

        try:
            return {
                "id": request.state.user.id,
                "fullname": request.state.user.fullname,
                "email": request.state.user.email,
            }
        except Exception as e:
            logging.warning(f"Failed to get user: {e}")
            return None

    def get_object_ids(self, request):
        object_ids = {}
        try:
            for key, value in request.path_params.items():
                object_ids[key] = value

            for key, value in request.query_params.items():
                object_ids[key] = value

            return object_ids
        except Exception as e:
            logging.warning(f"Failed to get object ids: {e}")
            return {}

    def get_agent(self, request):
        if not hasattr(request.state, "agent") or not request.state.agent:
            return None

        return {
            "id": request.state.agent.id,
            "version": request.state.agent.version,
            "name": request.state.agent.name,
            "temperature": request.state.agent.temperature,
            "model": request.state.agent.model,
            "deployment": request.state.agent.deployment,
            "description": request.state.agent.description,
            "preamble": request.state.agent.preamble,
            "tools": request.state.agent.tools,
        }


async def report_metrics(data):
    if not isinstance(data, dict):
        data = to_dict(data)

    data["secret"] = "secret"
    signal = {"signal": data}
    logging.info(signal)

    if not REPORT_ENDPOINT:
        logging.error("No report endpoint set")
        return

    transport = AsyncHTTPTransport(retries=NUM_RETRIES)
    try:
        async with AsyncClient(transport=transport) as client:
            await client.post(REPORT_ENDPOINT, json=signal)
    except Exception as e:
        logging.error(f"Failed to report metrics: {e}")


# DECORATORS
def collect_metrics_chat(func: Callable) -> Callable:
    @wraps(func)
    async def wrapper(self, chat_request: CohereChatRequest, **kwargs: Any) -> Any:
        start_time = time.perf_counter()
        metrics_data = initialize_metrics_data("chat", chat_request, **kwargs)

        response_dict = {}
        try:
            response = func(self, chat_request, **kwargs)
            response_dict = to_dict(response)
        except Exception as e:
            metrics_data = handle_error(metrics_data, e)
            raise e
        finally:
            metrics_data.input_tokens, metrics_data.output_tokens = (
                get_input_output_tokens(response_dict)
            )
            metrics_data.duration_ms = time.perf_counter() - start_time
            run_loop(metrics_data)

            return response_dict

    return wrapper


def collect_metrics_chat_stream(func: Callable) -> Callable:
    @wraps(func)
    def wrapper(self, chat_request: CohereChatRequest, **kwargs: Any) -> Any:
        start_time = time.perf_counter()
        metrics_data, kwargs = initialize_metrics_data("chat", chat_request, **kwargs)

        stream = func(self, chat_request, **kwargs)

        try:
            for event in stream:
                event_dict = to_dict(event)

                if is_event_end_with_error(event_dict):
                    metrics_data.success = False
                    metrics_data.error = event_dict.get("error")

                if event_dict.get("event_type") == StreamEvent.STREAM_END:
                    metrics_data.input_nb_tokens, metrics_data.output_nb_tokens = (
                        get_input_output_tokens(event_dict.get("response"))
                    )

                yield event_dict
        except Exception as e:
            metrics_data = handle_error(metrics_data, e)
            raise e
        finally:
            metrics_data.duration_ms = time.perf_counter() - start_time
            run_loop(metrics_data)

    return wrapper


def collect_metrics_rerank(func: Callable) -> Callable:
    @wraps(func)
    def wrapper(self, query: str, documents: Dict[str, Any], **kwargs: Any) -> Any:
        start_time = time.perf_counter()
        metrics_data, kwargs = initialize_metrics_data("rerank", None, **kwargs)

        response_dict = {}
        try:
            response = func(self, query, documents, **kwargs)
            response_dict = to_dict(response)
            metrics_data.search_units = get_search_units(response_dict)
        except Exception as e:
            metrics_data = handle_error(metrics_data, e)
            raise e
        finally:
            metrics_data.duration_ms = time.perf_counter() - start_time
            run_loop(metrics_data)
            return response_dict

    return wrapper


def run_loop(metrics_data):
    # Don't report metrics if no data or endpoint is set
    if not metrics_data or not REPORT_ENDPOINT:
        logging.warning("No metrics data or endpoint set")
        return

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(report_metrics(metrics_data))
    except RuntimeError:
        asyncio.run(report_metrics(metrics_data))


def initialize_metrics_data(
    func_name: str, chat_request: CohereChatRequest, **kwargs: Any
) -> tuple[MetricsData, Any]:
    return (
        MetricsData(
            endpoint_name=f"co.{func_name}",
            method="POST",
            trace_id=kwargs.pop("trace_id", None),
            user_id=kwargs.pop("user_id", None),
            assistant_id=kwargs.pop("agent_id", None),
            model=chat_request.model if chat_request else None,
            success=True,
        ),
        kwargs,
    )


def get_input_output_tokens(response_dict: dict) -> tuple[int, int]:
    if response_dict is None:
        return None, None

    input_tokens = (
        response_dict.get("meta", {}).get("billed_units", {}).get("input_tokens")
    )
    output_tokens = (
        response_dict.get("meta", {}).get("billed_units", {}).get("output_tokens")
    )
    return input_tokens, output_tokens


def get_search_units(response_dict: dict) -> int:
    return response_dict.get("meta", {}).get("billed_units", {}).get("search_units")


def is_event_end_with_error(event_dict: dict) -> bool:
    return (
        event_dict.get("event_type") == StreamEvent.STREAM_END
        and event_dict.get("finish_reason") != "COMPLETE"
        and event_dict.get("finish_reason") != "MAX_TOKENS"
    )


def handle_error(metrics_data: MetricsData, e: Exception) -> None:
    metrics_data.success = False
    metrics_data.error = str(e)
    if isinstance(e, ApiError):
        metrics_data.status_code = e.status_code
    return metrics_data
