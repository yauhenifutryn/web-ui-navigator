from __future__ import annotations

from typing import Any

import httpx

from marketplace_bot.navigator_models import ApprovalRequest, CreateSessionRequest, ExecuteResultPayload, ObservationPacket


class RemoteNavigatorClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    async def health(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(f"{self.base_url}/health")
            response.raise_for_status()
            return response.json()

    async def list_sessions(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"{self.base_url}/sessions")
            response.raise_for_status()
            return response.json()

    async def create_session(self, request: CreateSessionRequest) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(f"{self.base_url}/sessions", json=request.model_dump(mode="json"))
            response.raise_for_status()
            return response.json()

    async def observe(self, payload: ObservationPacket) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(f"{self.base_url}/observe", json=payload.model_dump(mode="json"))
            response.raise_for_status()
            return response.json()

    async def index_site(self, session_id: str, observation: ObservationPacket) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{self.base_url}/index-site",
                json={"session_id": session_id, "observation": observation.model_dump(mode="json")},
            )
            response.raise_for_status()
            return response.json()

    async def plan(self, session_id: str, observation: ObservationPacket | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"session_id": session_id}
        if observation is not None:
            payload["observation"] = observation.model_dump(mode="json")
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(f"{self.base_url}/plan", json=payload)
            response.raise_for_status()
            return response.json()

    async def execute_result(self, payload: ExecuteResultPayload) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(f"{self.base_url}/execute-result", json=payload.model_dump(mode="json"))
            response.raise_for_status()
            return response.json()

    async def get_session(self, session_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"{self.base_url}/sessions/{session_id}")
            response.raise_for_status()
            return response.json()

    async def resume_session(self, session_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(f"{self.base_url}/sessions/{session_id}/resume")
            response.raise_for_status()
            return response.json()

    async def approve(self, session_id: str, payload: ApprovalRequest) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(f"{self.base_url}/sessions/{session_id}/approve", json=payload.model_dump(mode="json"))
            response.raise_for_status()
            return response.json()
