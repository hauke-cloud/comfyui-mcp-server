from __future__ import annotations

import base64
import json

import anyio
import pytest

from comfyui_mcp_server.comfyui import ComfyUIError
from comfyui_mcp_server.workflows import WorkflowInputError


async def test_run_to_completion(make_services, fake_comfyui) -> None:
    services = make_services()
    workflow = services.registry.get("txt2img")
    job_id, values = await services.runner.submit(workflow, {"prompt": "a lighthouse", "steps": 4})

    graph = fake_comfyui.prompts[job_id]
    assert graph["6"]["inputs"]["text"] == "a lighthouse"
    assert graph["3"]["inputs"]["steps"] == 4
    assert graph["3"]["inputs"]["seed"] == values["seed"]
    # Requests keep the path prefix of the configured URL.
    assert fake_comfyui.requests[0].url.path == "/upstream/comfyui/prompt"

    result = await services.runner.wait(job_id, max_wait=5)
    assert result.status == "completed"
    assert [o.filename for o in result.outputs] == ["txt2img_00001_.png"], "outputs are limited to Save Image"
    assert result.outputs[0].url.startswith("https://mcp.example.com/api/view?filename=txt2img_00001_.png")
    await services.aclose()


async def test_temp_outputs_dropped_when_saved_outputs_exist(make_services) -> None:
    services = make_services()
    job_id = await services.runner.submit_graph(services.registry.get("txt2img").graph)
    result = await services.runner.wait(job_id, max_wait=5)
    assert {o.type for o in result.outputs} == {"output"}
    await services.aclose()


async def test_execution_error_is_reported(make_services, fake_comfyui) -> None:
    fake_comfyui.fail_with = {"node_id": "4", "node_type": "CheckpointLoaderSimple", "exception_message": "missing"}
    services = make_services()
    job_id, _ = await services.runner.submit(services.registry.get("txt2img"), {"prompt": "x"})
    result = await services.runner.wait(job_id, max_wait=5)
    assert result.status == "failed"
    assert result.error == fake_comfyui.fail_with
    await services.aclose()


async def test_rejected_prompt_raises(make_services, fake_comfyui) -> None:
    fake_comfyui.reject_with = {"error": {"message": "Prompt outputs failed validation", "details": ""}}
    services = make_services()
    with pytest.raises(ComfyUIError, match="failed validation"):
        await services.runner.submit(services.registry.get("txt2img"), {"prompt": "x"})
    await services.aclose()


async def test_timeout_returns_running_job(make_services, fake_comfyui) -> None:
    fake_comfyui.polls_until_done = 10_000
    services = make_services()
    job_id, _ = await services.runner.submit(services.registry.get("txt2img"), {"prompt": "x"})
    result = await services.runner.wait(job_id, max_wait=0.05)
    assert result.status == "running"
    assert not result.finished
    await services.aclose()


async def test_lost_job_fails(make_services) -> None:
    services = make_services()
    result = await services.runner.wait("does-not-exist", max_wait=5)
    assert result.status == "failed"
    assert "disappeared" in result.error["message"]
    await services.aclose()


async def test_image_inputs_are_uploaded(make_services, fake_comfyui) -> None:
    services = make_services()
    workflow = services.registry.get("img2img")
    data_uri = "data:image/png;base64," + base64.b64encode(b"png-bytes").decode()
    job_id, values = await services.runner.submit(workflow, {"image": data_uri, "prompt": "x"})
    assert values["image"] == "upload_0.png"
    assert fake_comfyui.prompts[job_id]["10"]["inputs"]["image"] == "upload_0.png"
    assert b"png-bytes" in fake_comfyui.uploads["upload_0.png"]

    # A previous output is fetched from /view and re-uploaded as an input.
    job_id, values = await services.runner.submit(workflow, {"image": "output:mcp/a.png", "prompt": "x"})
    assert values["image"] == "upload_1.png"
    view = next(r for r in fake_comfyui.requests if r.url.path.endswith("/view"))
    assert dict(view.url.params) == {"filename": "a.png", "subfolder": "mcp", "type": "output"}

    # Files already in ComfyUI's input directory are referenced as-is.
    job_id, values = await services.runner.submit(workflow, {"image": "input:photo.jpg", "prompt": "x"})
    assert fake_comfyui.prompts[job_id]["10"]["inputs"]["image"] == "photo.jpg"
    await services.aclose()


async def test_url_inputs_disabled_by_default(make_services) -> None:
    services = make_services()
    with pytest.raises(WorkflowInputError, match="URL inputs are disabled"):
        await services.runner.submit(
            services.registry.get("img2img"), {"image": "http://169.254.169.254/latest", "prompt": "x"}
        )
    with pytest.raises(WorkflowInputError, match="not valid base64"):
        await services.runner.submit(services.registry.get("img2img"), {"image": "not base64!", "prompt": "x"})
    await services.aclose()


async def test_cancel_running_job(make_services, fake_comfyui) -> None:
    fake_comfyui.polls_until_done = 10_000
    services = make_services()
    job_id, _ = await services.runner.submit(services.registry.get("txt2img"), {"prompt": "x"})
    await services.runner.cancel(job_id)
    interrupt = next(r for r in fake_comfyui.requests if r.url.path.endswith("/interrupt"))
    assert json.loads(interrupt.content) == {"prompt_id": job_id}
    await services.aclose()


def _cold_start(services, seconds: float) -> None:
    """Hold the first request open, as llama-swap does while ComfyUI starts."""
    queue_prompt = services.client.queue_prompt

    async def slow(graph):
        await anyio.sleep(seconds)
        return await queue_prompt(graph)

    services.client.queue_prompt = slow


async def test_submit_reports_progress_during_cold_start(make_services) -> None:
    services = make_services()
    _cold_start(services, 0.1)
    reports: list[tuple[float, str]] = []

    async def on_progress(elapsed: float, message: str) -> None:
        reports.append((elapsed, message))

    job_id, _ = await services.runner.submit(services.registry.get("txt2img"), {"prompt": "x"}, on_progress=on_progress)
    assert job_id
    assert len(reports) >= 3
    assert all("starting up" in message for _, message in reports)
    assert [elapsed for elapsed, _ in reports] == sorted(elapsed for elapsed, _ in reports)
    await services.aclose()


async def test_submit_errors_survive_the_heartbeat(make_services, fake_comfyui) -> None:
    services = make_services()
    _cold_start(services, 0.05)
    fake_comfyui.reject_with = {"error": {"message": "Prompt outputs failed validation"}}

    async def on_progress(elapsed: float, message: str) -> None:
        pass

    with pytest.raises(ComfyUIError, match="failed validation"):
        await services.runner.submit(services.registry.get("txt2img"), {"prompt": "x"}, on_progress=on_progress)
    await services.aclose()
