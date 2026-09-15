"""Submitting workflows to ComfyUI and turning its history into results."""

from __future__ import annotations

import base64
import binascii
import logging
import mimetypes
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlencode

import anyio
import httpx2

from .comfyui import VIEW_TYPES, ComfyUIClient, ComfyUIError
from .config import Settings
from .workflows import Workflow, WorkflowInputError

logger = logging.getLogger(__name__)

MAX_URL_INPUT_BYTES = 50 * 1024 * 1024
# A job missing from both queue and history this many polls in a row is gone,
# typically because ComfyUI restarted (e.g. llama-swap swapped it out).
LOST_AFTER_POLLS = 3

ProgressCallback = Callable[[float, str], Awaitable[None]]


@dataclass
class OutputFile:
    node_id: str
    kind: str
    filename: str
    subfolder: str
    type: str
    url: str

    @property
    def mime_type(self) -> str:
        return mimetypes.guess_type(self.filename)[0] or "application/octet-stream"


@dataclass
class JobResult:
    job_id: str
    status: str  # pending | running | completed | failed | cancelled | unknown
    workflow: str | None = None
    queue_position: int | None = None
    outputs: list[OutputFile] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    error: dict[str, Any] | None = None
    parameters: dict[str, Any] | None = None

    @property
    def finished(self) -> bool:
        return self.status in ("completed", "failed", "cancelled")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return {key: value for key, value in data.items() if value not in (None, [], {})}


@dataclass
class _JobInfo:
    workflow: str | None
    output_nodes: list[str]
    parameters: dict[str, Any] | None


class Runner:
    def __init__(self, settings: Settings, client: ComfyUIClient):
        self.settings = settings
        self.client = client
        # Remembers which workflow a job came from so status lookups can
        # filter outputs. Per replica and bounded; losing it only widens output.
        self._jobs: OrderedDict[str, _JobInfo] = OrderedDict()

    def _remember(self, job_id: str, info: _JobInfo) -> None:
        self._jobs[job_id] = info
        while len(self._jobs) > 1000:
            self._jobs.popitem(last=False)

    async def submit(
        self,
        workflow: Workflow,
        parameters: dict[str, Any] | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        values = workflow.validate(parameters)
        for name, value in list(values.items()):
            if workflow.parameters[name].type == "image":
                values[name] = await self.resolve_image(name, value)
        graph = workflow.build(values, overrides)
        job_id = await self.client.queue_prompt(graph)
        self._remember(job_id, _JobInfo(workflow.name, workflow.outputs, values))
        logger.info("queued workflow %s as job %s", workflow.name, job_id)
        return job_id, values

    async def submit_graph(self, graph: dict[str, Any]) -> str:
        job_id = await self.client.queue_prompt(graph)
        self._remember(job_id, _JobInfo(None, [], None))
        return job_id

    async def status(self, job_id: str) -> JobResult:
        info = self._jobs.get(job_id, _JobInfo(None, [], None))
        result = JobResult(job_id=job_id, status="unknown", workflow=info.workflow, parameters=info.parameters)
        entry = await self.client.history(job_id)
        if entry is None:
            position = (await self.client.queue()).position(job_id)
            if position is not None:
                result.status = "running" if position == 0 else "pending"
                result.queue_position = position
            return result

        status = entry.get("status") or {}
        messages = status.get("messages") or []
        events = {event: data for event, data in messages if isinstance(data, dict)}
        if "execution_interrupted" in events:
            result.status = "cancelled"
        elif status.get("status_str") == "error" or "execution_error" in events:
            result.status = "failed"
            detail = events.get("execution_error", {})
            result.error = {
                key: detail[key]
                for key in ("node_id", "node_type", "exception_type", "exception_message")
                if key in detail
            } or {"message": "ComfyUI reported an error"}
        else:
            result.status = "completed"
        result.outputs, result.texts = self._collect_outputs(entry.get("outputs") or {}, info.output_nodes)
        return result

    async def wait(self, job_id: str, max_wait: float, on_progress: ProgressCallback | None = None) -> JobResult:
        started = time.monotonic()
        missing = 0
        last_message = ""
        while True:
            result = await self.status(job_id)
            if result.finished:
                return result
            missing = missing + 1 if result.status == "unknown" else 0
            if missing >= LOST_AFTER_POLLS:
                result.status = "failed"
                result.error = {"message": "job disappeared from ComfyUI's queue and history (ComfyUI restarted?)"}
                return result
            elapsed = time.monotonic() - started
            if elapsed >= max_wait:
                return result
            if on_progress:
                message = "running" if result.status == "running" else f"queued at position {result.queue_position}"
                if result.status == "unknown":
                    message = last_message or "waiting"
                last_message = message
                await on_progress(elapsed, message)
            await anyio.sleep(self.settings.poll_interval)

    async def cancel(self, job_id: str) -> None:
        await self.client.cancel(job_id)

    def _collect_outputs(self, outputs: dict[str, Any], output_nodes: list[str]) -> tuple[list[OutputFile], list[str]]:
        files: list[OutputFile] = []
        texts: list[str] = []
        for node_id, node_output in outputs.items():
            if output_nodes and node_id not in output_nodes:
                continue
            for kind, items in node_output.items():
                if not isinstance(items, list):
                    continue
                for item in items:
                    if isinstance(item, dict) and "filename" in item:
                        files.append(self._output_file(node_id, kind, item))
                    elif isinstance(item, str) and kind in ("text", "string"):
                        texts.append(item)
        # Preview nodes write to temp; only fall back to those when nothing was saved.
        if not output_nodes and any(f.type != "temp" for f in files):
            files = [f for f in files if f.type != "temp"]
        return files, texts

    def _output_file(self, node_id: str, kind: str, item: dict[str, Any]) -> OutputFile:
        filename = item["filename"]
        subfolder = item.get("subfolder", "")
        type_ = item.get("type", "output")
        query = urlencode({"filename": filename, "subfolder": subfolder, "type": type_})
        return OutputFile(node_id, kind, filename, subfolder, type_, f"{self.settings.public_url}/api/view?{query}")

    async def inline_images(self, outputs: list[OutputFile]) -> list[tuple[bytes, str]]:
        """Download image outputs until the configured byte budget is spent."""
        if not self.settings.inline_images:
            return []
        budget = self.settings.inline_max_bytes
        images = []
        for output in outputs:
            if not output.mime_type.startswith("image/"):
                continue
            response = await self.client.view(output.filename, output.subfolder, output.type)
            if len(response.content) > budget:
                continue
            budget -= len(response.content)
            images.append((response.content, response.headers.get("content-type", output.mime_type)))
        return images

    async def resolve_image(self, name: str, value: str) -> str:
        """Turn an image parameter into a filename in ComfyUI's input directory."""
        prefix, sep, rest = value.partition(":")
        if sep and prefix == "input":
            return rest
        if sep and prefix in VIEW_TYPES:
            subfolder, _, filename = rest.rpartition("/")
            data = (await self.client.view(filename, subfolder, prefix)).content
            return await self.client.upload(data, _upload_name(filename))
        if value.startswith(("http://", "https://")):
            if not self.settings.allow_url_inputs:
                raise WorkflowInputError([f"{name}: URL inputs are disabled on this server (ALLOW_URL_INPUTS)"])
            data, mime = await _download(value)
            return await self.client.upload(data, _upload_name(value.split("?")[0], mime))
        mime = None
        if value.startswith("data:"):
            header, _, value = value.partition(",")
            mime = header[5:].split(";")[0] or None
        try:
            data = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            problem = f"{name}: not valid base64, a data: URI, a URL or an input:/output: reference"
            raise WorkflowInputError([problem]) from exc
        return await self.client.upload(data, _upload_name("", mime))


def _upload_name(original: str, mime: str | None = None) -> str:
    suffix = ""
    if "." in original.rsplit("/", 1)[-1]:
        suffix = "." + original.rsplit(".", 1)[-1]
    elif mime:
        suffix = mimetypes.guess_extension(mime) or ""
    return f"mcp_{uuid.uuid4().hex}{suffix or '.png'}"


async def _download(url: str) -> tuple[bytes, str | None]:
    # A fresh client, so ComfyUI credentials never travel to third-party hosts.
    async with httpx2.AsyncClient(timeout=60.0, follow_redirects=True) as http:
        try:
            async with http.stream("GET", url) as response:
                response.raise_for_status()
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_URL_INPUT_BYTES:
                        raise ComfyUIError(f"input {url} exceeds {MAX_URL_INPUT_BYTES} bytes", status=400)
                    chunks.append(chunk)
                return b"".join(chunks), response.headers.get("content-type")
        except httpx2.HTTPError as exc:
            raise ComfyUIError(f"could not fetch {url}: {exc}", status=400) from exc
