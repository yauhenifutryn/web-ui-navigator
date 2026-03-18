from __future__ import annotations

import json
from pathlib import Path

from marketplace_bot.navigator_models import SessionMemory

try:
    from google.cloud import firestore
except Exception:  # pragma: no cover
    firestore = None


class LocalJsonSessionRepository:
    def __init__(self, runtime_dir: Path) -> None:
        self.base_dir = runtime_dir / "sessions"
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save(self, session: SessionMemory) -> SessionMemory:
        path = self.base_dir / f"{session.session_id}.json"
        path.write_text(session.model_dump_json(indent=2), encoding="utf-8")
        return session

    def get(self, session_id: str) -> SessionMemory | None:
        path = self.base_dir / f"{session_id}.json"
        if not path.exists():
            return None
        return SessionMemory.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def list(self) -> list[SessionMemory]:
        sessions: list[SessionMemory] = []
        for path in sorted(self.base_dir.glob("*.json")):
            try:
                sessions.append(SessionMemory.model_validate(json.loads(path.read_text(encoding="utf-8"))))
            except Exception:
                continue
        return sorted(sessions, key=lambda item: item.updated_at, reverse=True)


class FirestoreSessionRepository:
    def __init__(self, collection_name: str = "navigator_sessions") -> None:
        if firestore is None:
            raise RuntimeError("google-cloud-firestore is not installed")
        self.client = firestore.Client()
        self.collection = self.client.collection(collection_name)

    def save(self, session: SessionMemory) -> SessionMemory:
        self.collection.document(session.session_id).set(session.model_dump(mode="json"))
        return session

    def get(self, session_id: str) -> SessionMemory | None:
        snapshot = self.collection.document(session_id).get()
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        return SessionMemory.model_validate(data)

    def list(self) -> list[SessionMemory]:
        rows = self.collection.order_by("updated_at", direction=firestore.Query.DESCENDING).limit(50).stream()
        return [SessionMemory.model_validate(row.to_dict() or {}) for row in rows]


class HybridSessionRepository:
    def __init__(self, local_repo: LocalJsonSessionRepository, cloud_repo: FirestoreSessionRepository | None = None) -> None:
        self.local_repo = local_repo
        self.cloud_repo = cloud_repo

    def save(self, session: SessionMemory) -> SessionMemory:
        self.local_repo.save(session)
        if self.cloud_repo is not None:
            try:
                self.cloud_repo.save(session)
            except Exception:
                pass
        return session

    def get(self, session_id: str) -> SessionMemory | None:
        local = self.local_repo.get(session_id)
        if local is not None:
            return local
        if self.cloud_repo is not None:
            try:
                cloud = self.cloud_repo.get(session_id)
            except Exception:
                cloud = None
            if cloud is not None:
                self.local_repo.save(cloud)
            return cloud
        return None

    def list(self) -> list[SessionMemory]:
        local = self.local_repo.list()
        if local:
            return local
        if self.cloud_repo is not None:
            try:
                cloud = self.cloud_repo.list()
            except Exception:
                cloud = []
            for item in cloud:
                self.local_repo.save(item)
            return cloud
        return []
