"""MCP surface: one tool per workflow plus a handful of job tools."""

from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import mcp_types as types
from mcp.server import Server, ServerRequestContext
from mcp.shared.exceptions import MCPError

from . import __version__
from .comfyui import ComfyUIError
from .runner import JobResult, ProgressCallback
from .services import Services
from .workflows import WorkflowInputError

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
Generates images and other media with ComfyUI. Each workflow is its own tool
whose arguments are the workflow's parameters; call list_workflows for an
overview. Workflow tools wait for the result and return the generated files
(inline images plus download URLs). A long job returns its job_id with status
pending or running instead: call get_job with that id to keep waiting. To feed
an output into another workflow's image parameter, pass
"output:<subfolder>/<filename>" using the values from the result."""

_JOB_ID = {"type": "string", "description": "Job id returned by a workflow tool"}
_WAIT = {
    "type": "boolean",
    "description": "Wait for the job to finish (up to the server's job timeout)",
    "default": True,
}
_READ_ONLY = types.ToolAnnotations(read_only_hint=True, open_world_hint=False)
_GENERATES = types.ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)

ToolHandler = Callable[[ServerRequestContext[Any], dict[str, Any]], Awaitable[types.CallToolResult]]


def _object(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


def _text(data: Any, *, is_error: bool = False) -> types.CallToolResult:
    text = data if isinstance(data, str) else json.dumps(data, indent=2, default=str)
    return types.CallToolResult(content=[types.TextContent(text=text)], is_error=is_error)


def create_mcp_server(services: Services) -> Server[Any]:
    settings, registry, runner = services.settings, services.registry, services.runner

    async def job_result(
        ctx: ServerRequestContext[Any], result: JobResult, wait: bool, started: float | None = None
    ) -> types.CallToolResult:
        if wait and not result.finished:
            # One budget for the whole call: time spent submitting (a ComfyUI
            # cold start) comes off the wait, so the call still returns the
            # job id before the client's tool timeout.
            spent = time.monotonic() - started if started is not None else 0.0
            result = await runner.wait(
                result.job_id, max(settings.job_timeout - spent, 0.0), _progress(ctx, offset=spent)
            )

        data = result.to_dict()
        content: list[types.ContentBlock] = [types.TextContent(text=json.dumps(data, indent=2))]
        if not result.finished:
            content.append(
                types.TextContent(text=f"Job is still {result.status}. Call get_job with job_id {result.job_id!r}.")
            )
        if result.status == "completed":
            for image, mime in await runner.inline_images(result.outputs):
                content.append(types.ImageContent(data=base64.b64encode(image).decode(), mime_type=mime))
        return types.CallToolResult(content=content, structured_content=data, is_error=result.status == "failed")

    async def list_workflows(ctx: ServerRequestContext[Any], args: dict[str, Any]) -> types.CallToolResult:
        data: dict[str, Any] = {"workflows": [w.summary() for w in registry.all().values()]}
        if registry.errors:
            data["load_errors"] = registry.errors
        return _text(data)

    async def run_workflow(ctx: ServerRequestContext[Any], args: dict[str, Any]) -> types.CallToolResult:
        workflow = registry.get(args.get("workflow", ""))
        if workflow is None:
            return _text(f"unknown workflow {args.get('workflow')!r}; call list_workflows", is_error=True)
        started = time.monotonic()
        job_id, values = await runner.submit(workflow, args.get("parameters"), args.get("overrides"), _progress(ctx))
        return await job_result(ctx, _queued(job_id, workflow.name, values), args.get("wait", True), started)

    async def run_raw_workflow(ctx: ServerRequestContext[Any], args: dict[str, Any]) -> types.CallToolResult:
        started = time.monotonic()
        job_id = await runner.submit_graph(args["workflow"], _progress(ctx))
        return await job_result(ctx, _queued(job_id, None, None), args.get("wait", True), started)

    async def get_job(ctx: ServerRequestContext[Any], args: dict[str, Any]) -> types.CallToolResult:
        return await job_result(ctx, await runner.status(args["job_id"]), args.get("wait", True))

    async def cancel_job(ctx: ServerRequestContext[Any], args: dict[str, Any]) -> types.CallToolResult:
        await runner.cancel(args["job_id"])
        return _text((await runner.status(args["job_id"])).to_dict())

    async def queue_status(ctx: ServerRequestContext[Any], args: dict[str, Any]) -> types.CallToolResult:
        state = await services.client.queue()
        return _text({"running": state.running, "pending": state.pending})

    builtins: dict[str, tuple[types.Tool, ToolHandler]] = {
        "list_workflows": (
            types.Tool(
                name="list_workflows",
                description="List the available workflows with their descriptions and parameter schemas.",
                input_schema=_object({}),
                annotations=_READ_ONLY,
            ),
            list_workflows,
        ),
        "run_workflow": (
            types.Tool(
                name="run_workflow",
                description=(
                    "Run a workflow by name. Prefer the workflow's own tool; this generic form also accepts "
                    "'overrides', raw '<node id or title>.<input>' addresses for workflows without parameters."
                ),
                input_schema=_object(
                    {
                        "workflow": {"type": "string", "description": "Workflow name"},
                        "parameters": {"type": "object", "description": "Workflow parameters"},
                        "overrides": {"type": "object", "description": "Raw node input overrides"},
                        "wait": _WAIT,
                    },
                    ["workflow"],
                ),
                annotations=_GENERATES,
            ),
            run_workflow,
        ),
        "get_job": (
            types.Tool(
                name="get_job",
                description="Get a job's status and outputs, by default waiting for it to finish.",
                input_schema=_object({"job_id": _JOB_ID, "wait": _WAIT}, ["job_id"]),
                annotations=_READ_ONLY,
            ),
            get_job,
        ),
        "cancel_job": (
            types.Tool(
                name="cancel_job",
                description="Cancel a pending job or interrupt a running one.",
                input_schema=_object({"job_id": _JOB_ID}, ["job_id"]),
                annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True),
            ),
            cancel_job,
        ),
        "queue_status": (
            types.Tool(
                name="queue_status",
                description="Show the job ids ComfyUI is running and has queued.",
                input_schema=_object({}),
                annotations=_READ_ONLY,
            ),
            queue_status,
        ),
    }
    if settings.enable_raw_workflows:
        builtins["run_raw_workflow"] = (
            types.Tool(
                name="run_raw_workflow",
                description="Run an arbitrary ComfyUI workflow graph in API format.",
                input_schema=_object(
                    {"workflow": {"type": "object", "description": "API-format workflow graph"}, "wait": _WAIT},
                    ["workflow"],
                ),
                annotations=_GENERATES,
            ),
            run_raw_workflow,
        )

    def workflow_tools() -> dict[str, types.Tool]:
        tools = {}
        for workflow in registry.all().values():
            if workflow.name in builtins:
                logger.warning("workflow %s shadows a built-in tool; use run_workflow for it", workflow.name)
                continue
            description = workflow.description or f"Run the ComfyUI workflow {workflow.name}."
            tools[workflow.name] = types.Tool(
                name=workflow.name,
                title=workflow.title or None,
                description=description,
                input_schema=workflow.input_schema(),
                annotations=_GENERATES,
            )
        return tools

    async def on_list_tools(
        ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[tool for tool, _ in builtins.values()] + list(workflow_tools().values()))

    async def on_call_tool(ctx: ServerRequestContext[Any], params: types.CallToolRequestParams) -> types.CallToolResult:
        args = params.arguments or {}
        try:
            if params.name in builtins:
                return await builtins[params.name][1](ctx, args)
            workflow = registry.get(params.name)
            if workflow is None:
                raise MCPError(types.INVALID_PARAMS, f"unknown tool {params.name!r}")
            started = time.monotonic()
            job_id, values = await runner.submit(workflow, args, on_progress=_progress(ctx))
            return await job_result(ctx, _queued(job_id, workflow.name, values), True, started)
        except WorkflowInputError as exc:
            return _text({"error": "invalid parameters", "problems": exc.problems}, is_error=True)
        except ComfyUIError as exc:
            return _text({"error": str(exc), "details": exc.details}, is_error=True)

    @asynccontextmanager
    async def lifespan(server: Server[Any]):
        # The HTTP client outlives any one MCP session; closing it here ties it
        # to the session manager, which runs for the life of the process.
        try:
            yield {}
        finally:
            await services.aclose()

    return Server(
        "comfyui-mcp-server",
        version=__version__,
        title="ComfyUI workflows",
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


def _progress(ctx: ServerRequestContext[Any], offset: float = 0.0) -> ProgressCallback:
    # Progress must only increase within a call, hence the offset for the
    # wait that follows a submission.
    async def report(elapsed: float, message: str) -> None:
        await ctx.session.report_progress(offset + elapsed, None, message)

    return report


def _queued(job_id: str, workflow: str | None, values: dict[str, Any] | None) -> JobResult:
    return JobResult(job_id=job_id, status="pending", workflow=workflow, parameters=values)
