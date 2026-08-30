from __future__ import annotations

import json

import httpx
import pytest

from looplane.cloudflare_client import CloudflareRunClient, CloudflareRunClientError


@pytest.mark.asyncio
async def test_cloudflare_run_client_starts_run_and_fetches_status() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/runs" and request.method == "POST":
            return httpx.Response(
                202,
                json={"runId": "run-1", "status": "queued"},
                request=request,
            )
        if request.url.path == "/v1/runs/run-1" and request.method == "GET":
            return httpx.Response(
                200,
                json={"runId": "run-1", "status": "completed"},
                request=request,
            )
        return httpx.Response(404, json={"error": "not_found"}, request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = CloudflareRunClient(
        base_url="https://control.example/",
        token="control-token",
        client=http,
    )

    accepted = await client.start_run({"instruction": "fix"})
    status = await client.status("run-1")

    assert accepted == {"runId": "run-1", "status": "queued"}
    assert status == {"runId": "run-1", "status": "completed"}
    assert [request.headers["authorization"] for request in requests] == [
        "Bearer control-token",
        "Bearer control-token",
    ]


@pytest.mark.asyncio
async def test_cloudflare_run_client_lists_model_profiles() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/model-profiles"
        assert request.headers["authorization"] == "Bearer control-token"
        return httpx.Response(
            200,
            json={
                "default": "openrouter-primary",
                "profiles": [
                    {
                        "id": "openrouter-primary",
                        "provider": "openrouter",
                        "protocol": "openai-chat",
                        "model": "gpt-5-mini",
                        "ready": True,
                    }
                ],
            },
            request=request,
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = CloudflareRunClient(
        base_url="https://control.example",
        token="control-token",
        client=http,
    )

    profiles = await client.model_profiles()

    assert profiles["default"] == "openrouter-primary"
    assert profiles["profiles"][0]["provider"] == "openrouter"
    assert profiles["profiles"][0]["ready"] is True


@pytest.mark.asyncio
async def test_cloudflare_run_client_attaches_to_sse_with_resume_cursor() -> None:
    seen_last_event_id: str | None = None

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_last_event_id
        seen_last_event_id = request.headers.get("last-event-id")
        assert request.url.path == "/v1/runs/run-1/events"
        assert request.url.query == b"stream=1"
        return httpx.Response(
            200,
            content=(
                ": heartbeat\n\n"
                "id: 2\n"
                "event: run.completed\n"
                'data: {"event_type":"run.completed","sequence":2}\n\n'
            ),
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = CloudflareRunClient(
        base_url="https://control.example",
        token="control-token",
        client=http,
    )

    events = [event async for event in client.attach_events("run-1", last_event_id=1)]

    assert seen_last_event_id == "1"
    assert len(events) == 1
    assert events[0].id == "2"
    assert events[0].event == "run.completed"
    assert events[0].data == {"event_type": "run.completed", "sequence": 2}


@pytest.mark.asyncio
async def test_cloudflare_run_client_lists_and_submits_approvals() -> None:
    seen_submit_body: dict[str, object] | None = None

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_submit_body
        if request.url.path == "/v1/runs/run-1/approvals" and request.method == "GET":
            return httpx.Response(
                200,
                json={"pending": [{"requestId": "approval-1"}], "decisions": []},
                request=request,
            )
        if request.url.path == "/v1/runs/run-1/approvals/approval-1":
            seen_submit_body = json.loads(request.content)
            return httpx.Response(
                200,
                json={"ok": True, "requestId": "approval-1", "decision": "allow_once"},
                request=request,
            )
        return httpx.Response(404, json={"error": "not_found"}, request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = CloudflareRunClient(
        base_url="https://control.example",
        token="control-token",
        client=http,
    )

    approvals = await client.approvals("run-1")
    submitted = await client.submit_approval("run-1", "approval-1", "allow_once")

    assert approvals == {"pending": [{"requestId": "approval-1"}], "decisions": []}
    assert submitted == {"ok": True, "requestId": "approval-1", "decision": "allow_once"}
    assert seen_submit_body == {"decision": "allow_once"}


@pytest.mark.asyncio
async def test_cloudflare_run_client_bounds_error_text() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="x" * 2_000, request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = CloudflareRunClient(
        base_url="https://control.example",
        token="control-token",
        client=http,
    )

    with pytest.raises(CloudflareRunClientError) as exc:
        await client.status("run-1")

    assert exc.value.status_code == 502
    assert len(exc.value.error) <= 1_000 + len("[truncated]")
