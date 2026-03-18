from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException

from marketplace_bot.navigator_runtime import build_navigator_runtime
from marketplace_bot.navigator_models import ApprovalRequest, CreateSessionRequest, ExecuteResultPayload, ObservationPacket


def create_app() -> FastAPI:
    runtime = build_navigator_runtime()
    companion = runtime.companion

    app = FastAPI(title="Live Navigator Cloud Backend")
    app.state.session_repository = runtime.session_repository
    app.state.artifact_store = runtime.artifact_store
    app.state.companion = companion

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/sessions")
    async def list_sessions() -> dict[str, Any]:
        return {"sessions": [item.model_dump(mode="json") for item in companion.list_sessions()]}

    @app.post("/sessions")
    async def create_session(request: CreateSessionRequest) -> dict[str, Any]:
        session = await companion.create_session(request)
        return session.model_dump(mode="json")

    @app.get("/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        session = companion.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return session.model_dump(mode="json")

    @app.post("/sessions/{session_id}/resume")
    async def resume_session(session_id: str) -> dict[str, Any]:
        session = companion.resume_session(session_id)
        return session.model_dump(mode="json")

    @app.post("/observe")
    async def observe(payload: ObservationPacket) -> dict[str, Any]:
        session = companion.store_observation(payload)
        return session.model_dump(mode="json")

    @app.post("/index-site")
    async def index_site(payload: dict[str, Any]) -> dict[str, Any]:
        session_id = str(payload.get("session_id", "")).strip()
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        observation = payload.get("observation")
        if observation is None:
            raise HTTPException(status_code=400, detail="observation is required")
        packet = ObservationPacket.model_validate(observation)
        session = await companion.index_site(session_id, packet)
        return session.model_dump(mode="json")

    @app.post("/plan")
    async def plan(payload: dict[str, Any]) -> dict[str, Any]:
        session_id = str(payload.get("session_id", "")).strip()
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        session = companion.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        if not session.last_indexed_at or session.site_check_required:
            raise HTTPException(status_code=409, detail="Index the site first before requesting live advice.")
        observation = payload.get("observation")
        packet = ObservationPacket.model_validate(observation) if observation else None
        response = await companion.plan(session_id, packet)
        return response.model_dump(mode="json")

    @app.post("/execute-result")
    async def execute_result(payload: ExecuteResultPayload) -> dict[str, Any]:
        session = companion.record_execution(payload)
        return session.model_dump(mode="json")

    @app.post("/sessions/{session_id}/approve")
    async def approve(session_id: str, payload: ApprovalRequest) -> dict[str, Any]:
        selected = companion.approve_actions(session_id, payload.action_ids)
        return {"session_id": session_id, "approved_actions": selected}

    return app


app = create_app()
