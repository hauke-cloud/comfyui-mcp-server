"""REST API over the same workflows the MCP tools serve."""

from __future__ import annotations

import json
import time
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .comfyui import ComfyUIError
from .services import Services
from .workflows import WorkflowInputError


def _wait_flag(request: Request, default: bool) -> bool:
    raw = request.query_params.get("wait")
    return default if raw is None else raw.lower() in ("1", "true", "yes")


def _comfyui_error(exc: ComfyUIError) -> JSONResponse:
    # A 4xx from ComfyUI means the graph or input was bad; anything else is ours to report as a gateway failure.
    status = exc.status if exc.status and 400 <= exc.status < 500 else 502
    return JSONResponse({"error": str(exc), "details": exc.details}, status_code=status)


def create_routes(services: Services) -> list[Route]:
    settings, registry, runner = services.settings, services.registry, services.runner

    async def healthz(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def readyz(request: Request) -> Response:
        # Deliberately does not call ComfyUI: behind llama-swap any request
        # would cold-start it and evict whatever model is loaded.
        workflows = registry.all()
        return JSONResponse({"status": "ok", "workflows": len(workflows), "load_errors": registry.errors})

    async def list_workflows(request: Request) -> Response:
        return JSONResponse(
            {"workflows": [w.summary() for w in registry.all().values()], "load_errors": registry.errors}
        )

    async def get_workflow(request: Request) -> Response:
        workflow = registry.get(request.path_params["name"])
        if workflow is None:
            return JSONResponse({"error": "workflow not found"}, status_code=404)
        detail = workflow.summary()
        if request.query_params.get("graph") in ("1", "true"):
            detail["graph"] = workflow.graph
        return JSONResponse(detail)

    async def run_workflow(request: Request) -> Response:
        workflow = registry.get(request.path_params["name"])
        if workflow is None:
            return JSONResponse({"error": "workflow not found"}, status_code=404)
        try:
            body: Any = await request.json() if await request.body() else {}
        except json.JSONDecodeError:
            return JSONResponse({"error": "request body must be JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
        # Accept either the bare parameters or {"parameters": ..., "overrides": ...}.
        if "parameters" in body or "overrides" in body:
            parameters, overrides = body.get("parameters"), body.get("overrides")
        else:
            parameters, overrides = body, None
        try:
            started = time.monotonic()
            job_id, _ = await runner.submit(workflow, parameters, overrides)
            result = await runner.status(job_id)
            if _wait_flag(request, default=True):
                # A ComfyUI cold start during submission counts against the same budget.
                result = await runner.wait(job_id, max(settings.job_timeout - (time.monotonic() - started), 0.0))
        except WorkflowInputError as exc:
            return JSONResponse({"error": "invalid parameters", "problems": exc.problems}, status_code=422)
        except ComfyUIError as exc:
            return _comfyui_error(exc)
        return JSONResponse(result.to_dict(), status_code=200 if result.finished else 202)

    async def get_job(request: Request) -> Response:
        job_id = request.path_params["job_id"]
        try:
            if _wait_flag(request, default=False):
                result = await runner.wait(job_id, settings.job_timeout)
            else:
                result = await runner.status(job_id)
        except ComfyUIError as exc:
            return _comfyui_error(exc)
        status = 404 if result.status == "unknown" else 200
        return JSONResponse(result.to_dict(), status_code=status)

    async def cancel_job(request: Request) -> Response:
        job_id = request.path_params["job_id"]
        try:
            await runner.cancel(job_id)
            result = await runner.status(job_id)
        except ComfyUIError as exc:
            return _comfyui_error(exc)
        return JSONResponse(result.to_dict())

    async def queue(request: Request) -> Response:
        try:
            state = await services.client.queue()
        except ComfyUIError as exc:
            return _comfyui_error(exc)
        return JSONResponse({"running": state.running, "pending": state.pending})

    async def view(request: Request) -> Response:
        params = request.query_params
        if "filename" not in params:
            return JSONResponse({"error": "filename is required"}, status_code=400)
        try:
            upstream = await services.client.view(
                params["filename"], params.get("subfolder", ""), params.get("type", "output")
            )
        except ComfyUIError as exc:
            return _comfyui_error(exc)
        headers = {
            key: value
            for key, value in upstream.headers.items()
            if key.lower() in ("content-type", "content-disposition", "cache-control")
        }
        return Response(upstream.content, headers=headers)

    return [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/readyz", readyz, methods=["GET"]),
        Route("/api/workflows", list_workflows, methods=["GET"]),
        Route("/api/workflows/{name}", get_workflow, methods=["GET"]),
        Route("/api/workflows/{name}/run", run_workflow, methods=["POST"]),
        Route("/api/jobs/{job_id}", get_job, methods=["GET"]),
        Route("/api/jobs/{job_id}", cancel_job, methods=["DELETE"]),
        Route("/api/queue", queue, methods=["GET"]),
        Route("/api/view", view, methods=["GET"]),
    ]
