from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx2
import pytest

from comfyui_mcp_server.config import Settings
from comfyui_mcp_server.services import Services

ROOT = Path(__file__).resolve().parent.parent
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class FakeComfyUI:
    """Just enough of ComfyUI's HTTP API for the server's code paths."""

    def __init__(self) -> None:
        self.prompts: dict[str, dict[str, Any]] = {}
        self.uploads: dict[str, bytes] = {}
        self.polls_until_done = 1
        self.fail_with: dict[str, Any] | None = None
        self.reject_with: dict[str, Any] | None = None
        self._polls: dict[str, int] = {}
        self.cancelled: list[str] = []
        self.requests: list[httpx2.Request] = []

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/upstream/comfyui")
        if request.method == "POST" and path == "/prompt":
            if self.reject_with:
                return httpx2.Response(400, json=self.reject_with)
            prompt_id = str(uuid.uuid4())
            self.prompts[prompt_id] = json.loads(request.content)["prompt"]
            self._polls[prompt_id] = 0
            return httpx2.Response(200, json={"prompt_id": prompt_id, "number": 1, "node_errors": {}})
        if request.method == "GET" and path.startswith("/history/"):
            prompt_id = path.rsplit("/", 1)[1]
            if prompt_id not in self.prompts:
                return httpx2.Response(200, json={})
            self._polls[prompt_id] += 1
            if self._polls[prompt_id] <= self.polls_until_done:
                return httpx2.Response(200, json={})
            return httpx2.Response(200, json={prompt_id: self._history(prompt_id)})
        if request.method == "GET" and path == "/queue":
            running = [
                [0, pid, {}, {}, []]
                for pid, polls in self._polls.items()
                if polls <= self.polls_until_done and pid not in self.cancelled
            ]
            return httpx2.Response(200, json={"queue_running": running, "queue_pending": []})
        if request.method == "POST" and path == "/interrupt":
            self.cancelled.append(json.loads(request.content)["prompt_id"])
            return httpx2.Response(200)
        if request.method == "POST" and path == "/upload/image":
            name = f"upload_{len(self.uploads)}.png"
            self.uploads[name] = request.content
            return httpx2.Response(200, json={"name": name, "subfolder": "", "type": "input"})
        if request.method == "GET" and path == "/view":
            return httpx2.Response(200, content=PNG, headers={"content-type": "image/png"})
        return httpx2.Response(404, text="not found")

    def _history(self, prompt_id: str) -> dict[str, Any]:
        if self.fail_with:
            return {
                "outputs": {},
                "status": {
                    "status_str": "error",
                    "completed": False,
                    "messages": [["execution_start", {}], ["execution_error", self.fail_with]],
                },
            }
        return {
            "outputs": {
                "9": {"images": [{"filename": "txt2img_00001_.png", "subfolder": "mcp", "type": "output"}]},
                "12": {"images": [{"filename": "preview.png", "subfolder": "", "type": "temp"}]},
            },
            "status": {"status_str": "success", "completed": True, "messages": [["execution_success", {}]]},
        }


@pytest.fixture
def fake_comfyui() -> FakeComfyUI:
    return FakeComfyUI()


@pytest.fixture
def workflows_dir(tmp_path: Path) -> Path:
    target = tmp_path / "workflows"
    shutil.copytree(ROOT / "workflows", target)
    return target


@pytest.fixture
def settings(workflows_dir: Path) -> Settings:
    return Settings(
        comfyui_url="http://comfyui.test/upstream/comfyui",
        workflows_dirs=[workflows_dir],
        poll_interval=0.01,
        job_timeout=5,
        public_url="https://mcp.example.com",
    )


@pytest.fixture
def make_services(fake_comfyui: FakeComfyUI, settings: Settings):
    def factory(**overrides: Any) -> Services:
        return Services.create(replace(settings, **overrides), transport=fake_comfyui.transport())

    return factory
