"""Single entry point for LLM calls.

Rules (project standard): retry+backoff on transient errors, Pydantic validation of every output,
token/latency tracing on every call, no business logic in here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from text2sql.config.settings import Settings, get_settings
from text2sql.observability.tracing import LLMCallRecord, StructlogTracer, Tracer

T = TypeVar("T", bound=BaseModel)

_TOOL_NAME = "respond"
_AUTO_SUFFIX = f"Reply only by calling the `{_TOOL_NAME}` tool exactly once. Do not answer in plain text."


class LLMOutputError(RuntimeError):
    """The model did not return output that satisfies the requested schema."""


def _is_transient(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (anthropic.APIConnectionError, anthropic.APITimeoutError, anthropic.RateLimitError),
    ):
        return True
    return isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class StructuredResult(Generic[T]):
    parsed: T
    usage: Usage
    latency_ms: int
    model: str
    attempts: int


class LLMClient:
    def __init__(
        self,
        settings: Settings | None = None,
        sdk_client: Any | None = None,
        tracer: Tracer | None = None,
    ):
        self.settings = settings or get_settings()
        self.tracer: Tracer = tracer or StructlogTracer()
        if sdk_client is not None:
            self._sdk = sdk_client
        else:
            key = self.settings.anthropic_api_key
            if key is None:
                raise RuntimeError("ANTHROPIC_API_KEY is not set")
            self._sdk = anthropic.Anthropic(
                api_key=key.get_secret_value(),
                timeout=self.settings.llm_timeout_s,
                max_retries=0,  # retries are handled (and traced) here, not inside the SDK
            )
        self._no_forced_tool: set[str] = set()  # models that rejected tool_choice=tool

    # -- public ---------------------------------------------------------------------------
    def complete_structured(
        self,
        *,
        agent_name: str,
        system: str,
        user: str,
        response_model: type[T],
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> StructuredResult[T]:
        model = model or self.settings.llm_model_generation
        schema = response_model.model_json_schema()
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        usage = Usage()
        started = time.monotonic()
        last_error: str | None = None

        for attempt in range(1, self.settings.llm_max_output_attempts + 1):
            t0 = time.monotonic()
            try:
                resp = self._call_api(
                    model=model,
                    system=system,
                    messages=messages,
                    schema=schema,
                    description=response_model.__doc__ or "Structured response",
                    max_tokens=max_tokens or self.settings.llm_max_tokens,
                )
            except Exception as exc:
                self._trace(agent_name, model, Usage(), t0, False, attempt, repr(exc))
                raise
            call_usage = Usage(
                getattr(resp.usage, "input_tokens", 0), getattr(resp.usage, "output_tokens", 0)
            )
            usage.input_tokens += call_usage.input_tokens
            usage.output_tokens += call_usage.output_tokens

            try:
                payload = self._extract_tool_input(resp)
                parsed = response_model.model_validate(payload)
            except (LLMOutputError, ValidationError) as exc:
                last_error = str(exc)
                self._trace(agent_name, model, call_usage, t0, False, attempt, last_error[:500])
                messages = [
                    messages[0],
                    {"role": "assistant", "content": "(previous output rejected)"},
                    {
                        "role": "user",
                        "content": "Your previous response was invalid: "
                        f"{last_error[:800]}\nReturn a corrected response using the tool.",
                    },
                ]
                continue

            self._trace(agent_name, model, call_usage, t0, True, attempt, None)
            return StructuredResult(
                parsed=parsed,
                usage=usage,
                latency_ms=int((time.monotonic() - started) * 1000),
                model=model,
                attempts=attempt,
            )

        raise LLMOutputError(
            f"{agent_name}: no valid output after {self.settings.llm_max_output_attempts} attempts: "
            f"{last_error}"
        )

    # -- internals ------------------------------------------------------------------------
    def _call_api(self, *, model, system, messages, schema, description, max_tokens):
        @retry(
            retry=retry_if_exception(_is_transient),
            stop=stop_after_attempt(self.settings.llm_max_retries),
            wait=wait_exponential(multiplier=self.settings.llm_retry_wait_s, max=30),
            reraise=True,
        )
        def _do(forced: bool):
            return self._sdk.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system if forced else f"{system}\n\n{_AUTO_SUFFIX}",
                messages=messages,
                tools=[{"name": _TOOL_NAME, "description": description, "input_schema": schema}],
                tool_choice={"type": "tool", "name": _TOOL_NAME} if forced else {"type": "auto"},
            )

        if model not in self._no_forced_tool:
            try:
                return _do(True)
            except anthropic.BadRequestError as exc:
                if "tool_choice" not in str(exc):
                    raise
                # Some models (e.g. with adaptive thinking) refuse forced tool use: remember and use auto.
                self._no_forced_tool.add(model)
        return _do(False)

    @staticmethod
    def _extract_tool_input(resp: Any) -> dict:
        for block in getattr(resp, "content", []):
            if getattr(block, "type", None) == "tool_use" and block.name == _TOOL_NAME:
                return block.input
        raise LLMOutputError("model returned no tool_use block")

    def _trace(self, agent, model, usage: Usage, t0, ok, attempt, error) -> None:
        self.tracer.record(
            LLMCallRecord(
                agent_name=agent,
                model=model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                latency_ms=int((time.monotonic() - t0) * 1000),
                success=ok,
                attempt=attempt,
                error=error,
            )
        )
