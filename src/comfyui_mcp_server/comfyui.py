"""Thin async client for the ComfyUI HTTP API."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import httpx2

VIEW_TYPES = ("output", "input", "temp")


class ComfyUIError(Exception):
    """ComfyUI rejected a request or could not be reached."""

    def __init__(self, message: str, *, status: int | None = None, details: Any = None):
        super().__init__(message)
        self.status = status
        self.details = details


@dataclass
class QueueState:
    running: list[str]
    pending: list[str]

    def position(self, prompt_id: str) -> int | None:
        """0 while running, 1..n while pending, None when not queued at all."""
        if prompt_id in self.running:
            return 0
        if prompt_id in self.pending:
            return self.pending.index(prompt_id) + 1
        return None


class ComfyUIClient:
    def __init__(
        self,
        base_url: str,
        *,
        api_key: str = "",
        timeout: float = 600.0,
        transport: httpx2.AsyncBaseTransport | None = None,
    ):
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        # A trailing slash makes relative request paths append to any prefix
        # (e.g. llama-swap's /upstream/comfyui) instead of replacing it.
        self._http = httpx2.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers=headers,
            timeout=httpx2.Timeout(timeout, connect=30.0),
            transport=transport,
        )
        self.client_id = uuid.uuid4().hex

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx2.Response:
        try:
            response = await self._http.request(method, path.lstrip("/"), **kwargs)
        except httpx2.HTTPError as exc:
            raise ComfyUIError(f"ComfyUI unreachable: {exc}") from exc
        if response.status_code >= 400:
            try:
                details: Any = response.json()
            except ValueError:
                details = response.text[:2000]
            raise ComfyUIError(
                _error_message(details) or f"ComfyUI returned HTTP {response.status_code} for {path}",
                status=response.status_code,
                details=details,
            )
        return response

    async def queue_prompt(self, workflow: dict[str, Any]) -> str:
        response = await self._request("POST", "prompt", json={"prompt": workflow, "client_id": self.client_id})
        body = response.json()
        if body.get("node_errors"):
            raise ComfyUIError("ComfyUI rejected the workflow", status=400, details=body)
        return body["prompt_id"]

    async def history(self, prompt_id: str) -> dict[str, Any] | None:
        response = await self._request("GET", f"history/{prompt_id}")
        return response.json().get(prompt_id)

    async def queue(self) -> QueueState:
        body = (await self._request("GET", "queue")).json()
        # Entries are [number, prompt_id, prompt, extra_data, outputs_to_execute].
        return QueueState(
            running=[item[1] for item in body.get("queue_running", [])],
            pending=[item[1] for item in sorted(body.get("queue_pending", []), key=lambda item: item[0])],
        )

    async def cancel(self, prompt_id: str) -> None:
        state = await self.queue()
        if prompt_id in state.pending:
            await self._request("POST", "queue", json={"delete": [prompt_id]})
        elif prompt_id in state.running:
            # Older ComfyUI ignores the body and interrupts whatever is running,
            # which is this prompt since we just saw it at the head of the queue.
            await self._request("POST", "interrupt", json={"prompt_id": prompt_id})

    async def upload(self, data: bytes, filename: str, *, subfolder: str = "") -> str:
        """Store a file in ComfyUI's input directory and return the name loaders expect."""
        files = {"image": (filename, data, "application/octet-stream")}
        form = {"type": "input", "overwrite": "true", "subfolder": subfolder}
        body = (await self._request("POST", "upload/image", files=files, data=form)).json()
        name = body["name"]
        return f"{body['subfolder']}/{name}" if body.get("subfolder") else name

    async def view(self, filename: str, subfolder: str = "", type_: str = "output") -> httpx2.Response:
        if type_ not in VIEW_TYPES:
            raise ComfyUIError(f"invalid file type {type_!r}", status=400)
        return await self._request("GET", "view", params={"filename": filename, "subfolder": subfolder, "type": type_})


def _error_message(details: Any) -> str:
    if not isinstance(details, dict):
        return ""
    error = details.get("error")
    if isinstance(error, dict):
        message = error.get("message", "")
        if error.get("details"):
            message = f"{message}: {error['details']}"
        return message
    return str(error) if error else ""
