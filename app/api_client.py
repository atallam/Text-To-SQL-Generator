"""Thin client for the Text-To-SQL API. The UI never talks to the database or the model directly."""

from __future__ import annotations

from typing import Any

import requests

from text2sql.config.settings import get_env


class ApiError(Exception):
    """An error the UI can show to a person. ``kind`` picks the wording; ``detail`` is the server's message."""

    def __init__(self, kind: str, detail: str, status: int | None = None, retry_after: int | None = None):
        super().__init__(detail)
        self.kind, self.detail, self.status, self.retry_after = kind, detail, status, retry_after


class ApiClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        session_id: str = "",
        timeout: float = 150.0,
    ):
        self.base_url = (base_url or get_env("API_URL", "http://localhost:8000") or "").rstrip("/")
        self.api_key = api_key if api_key is not None else (get_env("API_KEY") or "")
        self.session_id, self.timeout = session_id, timeout

    # -- plumbing ---------------------------------------------------------------------------
    def _request(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = requests.request(
                method, self.base_url + path, headers={"X-API-Key": self.api_key}, timeout=self.timeout, **kw
            )
        except requests.ConnectionError:
            raise ApiError("unreachable", f"Cannot reach the API at {self.base_url}.") from None
        except requests.Timeout:
            raise ApiError("timeout", "The request took too long.") from None
        if r.ok:
            return r.json()
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        if isinstance(detail, list):  # FastAPI validation errors
            detail = "; ".join(str(d.get("msg", d)) for d in detail)
        kind = {
            401: "auth",
            403: "forbidden",
            404: "not_found",
            413: "too_large",
            422: "invalid",
            429: "rate",
            502: "upstream",
            503: "unavailable",
        }.get(r.status_code, "error")
        retry = r.headers.get("Retry-After")
        raise ApiError(kind, str(detail), r.status_code, int(retry) if retry and retry.isdigit() else None)

    # -- endpoints --------------------------------------------------------------------------
    def health(self) -> dict[str, Any] | None:
        try:
            r = requests.get(self.base_url + "/healthz", timeout=5)
            return r.json()
        except (requests.RequestException, ValueError):
            return None

    def sources(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/sources")

    def schema(self, source: str) -> dict[str, Any]:
        return self._request("GET", "/v1/schema", params={"source": source})

    def query(
        self, question: str, source: str, clarifications: dict[str, int | str] | None = None
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/query",
            json={
                "question": question,
                "source": source,
                "clarifications": clarifications or {},
                "session_id": self.session_id,
            },
        )

    def run_sql(self, sql: str, source: str) -> dict[str, Any]:
        return self._request(
            "POST", "/v1/query/run-sql", json={"sql": sql, "source": source, "session_id": self.session_id}
        )

    def history(self, source: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"session_id": self.session_id, "limit": limit}
        if source:
            params["source"] = source
        return self._request("GET", "/v1/history", params=params)

    def history_item(self, audit_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/history/{audit_id}", params={"session_id": self.session_id})

    def feedback(
        self, audit_id: str, verdict: str, comment: str | None = None, corrected_sql: str | None = None
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/feedback",
            json={
                "audit_id": audit_id,
                "verdict": verdict,
                "comment": comment or None,
                "corrected_sql": corrected_sql or None,
                "session_id": self.session_id,
            },
        )
