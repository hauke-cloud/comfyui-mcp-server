"""The objects shared by the MCP and REST surfaces."""

from __future__ import annotations

from dataclasses import dataclass

import httpx2

from .comfyui import ComfyUIClient
from .config import Settings
from .runner import Runner
from .uploads import UploadSigner
from .workflows import WorkflowRegistry


@dataclass
class Services:
    settings: Settings
    client: ComfyUIClient
    registry: WorkflowRegistry
    runner: Runner
    uploads: UploadSigner

    @classmethod
    def create(cls, settings: Settings, transport: httpx2.AsyncBaseTransport | None = None) -> Services:
        client = ComfyUIClient(
            settings.comfyui_url,
            api_key=settings.comfyui_api_key,
            timeout=settings.comfyui_request_timeout,
            transport=transport,
        )
        return cls(
            settings,
            client,
            WorkflowRegistry(settings.workflows_dirs),
            Runner(settings, client),
            UploadSigner(settings.upload_secret or settings.api_token, settings.upload_url_ttl),
        )

    async def aclose(self) -> None:
        await self.client.aclose()
