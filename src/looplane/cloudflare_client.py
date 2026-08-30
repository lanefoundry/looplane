"""Small async client for the Cloudflare run control-plane API."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from looplane.runtime import bounded_text


class CloudflareRunClientError(RuntimeError):
    def __init__(self, status_code: int, error: str) -> None:
        super().__init__(f"Cloudflare run API failed with HTTP {status_code}: {error}")
        self.status_code = status_code
        self.error = error


@dataclass(frozen=True)
class CloudflareRunEvent:
    id: str | None
    event: str
    data: Any


class CloudflareRunClient:
    """Typed helper for starting and attaching to Cloudflare-hosted runs."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def start_run(self, request: Mapping[str, Any]) -> dict[str, Any]:
        response = await self._client.post(
            self._url("/v1/runs"),
            headers=self._headers(content_type="application/json"),
            json=dict(request),
        )
        return self._json_response(response)

    async def model_profiles(self) -> dict[str, Any]:
        """Return the authenticated operator-managed model profile catalog."""
        response = await self._client.get(
            self._url("/v1/model-profiles"),
            headers=self._headers(),
        )
        return self._json_response(response)

    async def status(self, run_id: str) -> dict[str, Any]:
        response = await self._client.get(
            self._url(f"/v1/runs/{run_id}"),
            headers=self._headers(),
        )
        return self._json_response(response)

    async def cancel(self, run_id: str) -> dict[str, Any]:
        response = await self._client.post(
            self._url(f"/v1/runs/{run_id}/cancel"),
            headers=self._headers(content_type="application/json"),
            json={},
        )
        return self._json_response(response)

    async def approvals(self, run_id: str) -> dict[str, Any]:
        response = await self._client.get(
            self._url(f"/v1/runs/{run_id}/approvals"),
            headers=self._headers(),
        )
        return self._json_response(response)

    async def submit_approval(self, run_id: str, approval_id: str, decision: str) -> dict[str, Any]:
        response = await self._client.post(
            self._url(f"/v1/runs/{run_id}/approvals/{approval_id}"),
            headers=self._headers(content_type="application/json"),
            json={"decision": decision},
        )
        return self._json_response(response)

    async def artifact(self, run_id: str, name: str) -> str:
        response = await self._client.get(
            self._url(f"/v1/runs/{run_id}/artifacts/{name}"),
            headers=self._headers(),
        )
        self._raise_for_status(response)
        return response.text

    async def events(self, run_id: str) -> tuple[Any, ...]:
        response = await self._client.get(
            self._url(f"/v1/runs/{run_id}/events"),
            headers=self._headers(),
        )
        self._raise_for_status(response)
        parsed: list[Any] = []
        for line in response.text.splitlines():
            if not line:
                continue
            try:
                parsed.append(json.loads(line))
            except json.JSONDecodeError:
                parsed.append(line)
        return tuple(parsed)

    async def attach_events(
        self,
        run_id: str,
        *,
        last_event_id: str | int | None = None,
    ) -> AsyncIterator[CloudflareRunEvent]:
        headers = self._headers()
        if last_event_id is not None:
            headers["Last-Event-ID"] = str(last_event_id)
        async with self._client.stream(
            "GET",
            self._url(f"/v1/runs/{run_id}/events?stream=1"),
            headers=headers,
        ) as response:
            self._raise_for_status(response)
            buffer = ""
            async for chunk in response.aiter_text():
                buffer += chunk
                while "\n\n" in buffer:
                    frame, buffer = buffer.split("\n\n", 1)
                    event = self._parse_sse_frame(frame)
                    if event is not None:
                        yield event
            event = self._parse_sse_frame(buffer)
            if event is not None:
                yield event

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _headers(self, *, content_type: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.token}"}
        if content_type is not None:
            headers["Content-Type"] = content_type
        return headers

    @staticmethod
    def _parse_sse_frame(frame: str) -> CloudflareRunEvent | None:
        event_id: str | None = None
        event_name = "message"
        data_lines: list[str] = []
        for line in frame.splitlines():
            if not line or line.startswith(":"):
                continue
            field, separator, value = line.partition(":")
            if separator:
                value = value.removeprefix(" ")
            if field == "id":
                event_id = value
            elif field == "event":
                event_name = value or "message"
            elif field == "data":
                data_lines.append(value)
        if not data_lines:
            return None
        data = "\n".join(data_lines)
        try:
            parsed: Any = json.loads(data)
        except json.JSONDecodeError:
            parsed = data
        return CloudflareRunEvent(id=event_id, event=event_name, data=parsed)

    def _json_response(self, response: httpx.Response) -> dict[str, Any]:
        self._raise_for_status(response)
        try:
            value = response.json()
        except json.JSONDecodeError as exc:
            raise CloudflareRunClientError(response.status_code, "invalid_json_response") from exc
        if not isinstance(value, dict):
            raise CloudflareRunClientError(response.status_code, "invalid_json_response")
        return value

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        error = ""
        try:
            value = response.json()
            if isinstance(value, dict) and isinstance(value.get("error"), str):
                error = value["error"]
        except json.JSONDecodeError:
            error = ""
        if not error:
            error = bounded_text(response.text or response.reason_phrase, 1_000)
        raise CloudflareRunClientError(response.status_code, error)
