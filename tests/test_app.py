from __future__ import annotations

import base64
import time
from contextlib import asynccontextmanager
from dataclasses import replace

import anyio
import httpx2
from mcp import Client

from comfyui_mcp_server.app import create_app
from comfyui_mcp_server.mcp_server import create_mcp_server


@asynccontextmanager
async def serve(make_services, settings):
    # A context manager rather than a fixture: the MCP session manager's task
    # group must be entered and exited in the same task.
    app = create_app(settings, make_services())
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


async def test_probes(make_services, settings) -> None:
    async with serve(make_services, settings) as http:
        assert (await http.get("/healthz")).json() == {"status": "ok"}
        assert (await http.get("/readyz")).json()["workflows"] == 2


async def test_rest_workflow_run(make_services, settings) -> None:
    async with serve(make_services, settings) as http:
        listing = (await http.get("/api/workflows")).json()
        assert {w["name"] for w in listing["workflows"]} == {"txt2img", "img2img"}

        response = await http.post("/api/workflows/txt2img/run", json={"prompt": "a fox"})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "completed"
        assert body["outputs"][0]["filename"] == "txt2img_00001_.png"

        job = await http.get(f"/api/jobs/{body['job_id']}")
        assert job.json()["status"] == "completed"

        image = await http.get("/api/view", params={"filename": "txt2img_00001_.png", "subfolder": "mcp"})
        assert image.headers["content-type"] == "image/png"


async def test_rest_async_run_and_errors(make_services, settings) -> None:
    async with serve(make_services, settings) as http:
        response = await http.post("/api/workflows/txt2img/run?wait=false", json={"parameters": {"prompt": "x"}})
        assert response.status_code == 202
        assert response.json()["status"] in ("pending", "running")

        invalid = await http.post("/api/workflows/txt2img/run", json={"steps": "many"})
        assert invalid.status_code == 422
        assert len(invalid.json()["problems"]) == 2

        assert (await http.post("/api/workflows/missing/run", json={})).status_code == 404
        assert (await http.get("/api/jobs/unknown")).status_code == 404


async def test_bearer_token(make_services, settings) -> None:
    async with serve(make_services, replace(settings, api_token="s3cret")) as http:
        assert (await http.get("/healthz")).status_code == 200
        assert (await http.get("/api/workflows")).status_code == 401
        assert (await http.post("/mcp", json={})).status_code == 401
        ok = await http.get("/api/workflows", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200


async def test_mcp_over_streamable_http(make_services, settings) -> None:
    async with serve(make_services, settings) as http:
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        }
        response = await http.post("/mcp", json=init, headers=headers)
        assert response.status_code == 200
        assert "comfyui-mcp-server" in response.text


async def test_mcp_tools(make_services) -> None:
    services = make_services()
    async with Client(create_mcp_server(services)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert {"list_workflows", "run_workflow", "get_job", "cancel_job", "queue_status"} <= set(tools)
        assert "run_raw_workflow" not in tools
        assert tools["txt2img"].input_schema["required"] == ["prompt"]

        result = await client.call_tool("txt2img", {"prompt": "a robot", "width": 768})
        assert not result.is_error
        assert result.structured_content["status"] == "completed"
        images = [block for block in result.content if block.type == "image"]
        assert base64.b64decode(images[0].data).startswith(b"\x89PNG")

        invalid = await client.call_tool("txt2img", {"width": 1})
        assert invalid.is_error
        assert "prompt: required" in invalid.content[0].text

        queued = await client.call_tool(
            "run_workflow", {"workflow": "txt2img", "parameters": {"prompt": "x"}, "wait": False}
        )
        job_id = queued.structured_content["job_id"]
        finished = await client.call_tool("get_job", {"job_id": job_id})
        assert finished.structured_content["status"] == "completed"


async def test_mcp_raw_workflows_opt_in(make_services) -> None:
    services = make_services(enable_raw_workflows=True, inline_images=False)
    graph = services.registry.get("txt2img").graph
    async with Client(create_mcp_server(services)) as client:
        result = await client.call_tool("run_raw_workflow", {"workflow": graph})
        assert result.structured_content["status"] == "completed"
        assert all(block.type == "text" for block in result.content)


async def test_mcp_cold_start_counts_against_job_timeout(make_services, fake_comfyui) -> None:
    services = make_services(job_timeout=0.3)
    fake_comfyui.polls_until_done = 10_000
    queue_prompt = services.client.queue_prompt

    async def cold_start(graph):
        await anyio.sleep(0.5)
        return await queue_prompt(graph)

    services.client.queue_prompt = cold_start
    reports: list[tuple[float, str | None]] = []

    async def on_progress(progress: float, total: float | None, message: str | None) -> None:
        reports.append((progress, message))

    async with Client(create_mcp_server(services)) as client:
        started = time.monotonic()
        result = await client.call_tool("txt2img", {"prompt": "x"}, progress_callback=on_progress)
        # The cold start used up the budget, so the job id comes back without further waiting.
        assert time.monotonic() - started < 1.0
    assert result.structured_content["status"] == "running"
    assert "get_job" in result.content[1].text
    assert any("starting up" in (message or "") for _, message in reports)
