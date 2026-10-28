"""LLM call tracing. Every call records agent_name, model, tokens, latency, success.

A Langfuse adapter will implement the same Tracer protocol; the pipeline only sees Tracer.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Protocol

from text2sql.observability.logging import get_logger


@dataclass
class LLMCallRecord:
    agent_name: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    success: bool
    attempt: int = 1
    error: str | None = None


class Tracer(Protocol):
    def record(self, call: LLMCallRecord) -> None: ...


class StructlogTracer:
    def __init__(self) -> None:
        self._log = get_logger("llm")

    def record(self, call: LLMCallRecord) -> None:
        (self._log.info if call.success else self._log.warning)("llm_call", **asdict(call))


class NullTracer:
    def record(self, call: LLMCallRecord) -> None:
        return None


class MemoryTracer:
    """For tests."""

    def __init__(self) -> None:
        self.calls: list[LLMCallRecord] = []

    def record(self, call: LLMCallRecord) -> None:
        self.calls.append(call)
