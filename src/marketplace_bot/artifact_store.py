from __future__ import annotations

import base64
import json
from pathlib import Path

try:
    from google.cloud import storage
except Exception:  # pragma: no cover
    storage = None


class LocalArtifactStore:
    def __init__(self, runtime_dir: Path) -> None:
        self.base_dir = runtime_dir / "artifacts"
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save_png_b64(self, session_id: str, image_b64: str, filename: str) -> dict[str, str]:
        session_dir = self.base_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        path = session_dir / filename
        path.write_bytes(base64.b64decode(image_b64))
        return {"path": str(path), "uri": str(path)}

    def save_json(self, session_id: str, payload: dict, filename: str) -> dict[str, str]:
        session_dir = self.base_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        path = session_dir / filename
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return {"path": str(path), "uri": str(path)}


class GCSArtifactStore:
    def __init__(self, bucket_name: str) -> None:
        if storage is None:
            raise RuntimeError("google-cloud-storage is not installed")
        self.client = storage.Client()
        self.bucket = self.client.bucket(bucket_name)

    def save_png_b64(self, session_id: str, image_b64: str, filename: str) -> dict[str, str]:
        blob = self.bucket.blob(f"navigator/{session_id}/{filename}")
        blob.upload_from_string(base64.b64decode(image_b64), content_type="image/png")
        return {"path": f"gs://{self.bucket.name}/{blob.name}", "uri": f"gs://{self.bucket.name}/{blob.name}"}

    def save_json(self, session_id: str, payload: dict, filename: str) -> dict[str, str]:
        blob = self.bucket.blob(f"navigator/{session_id}/{filename}")
        blob.upload_from_string(json.dumps(payload, indent=2, sort_keys=True), content_type="application/json")
        return {"path": f"gs://{self.bucket.name}/{blob.name}", "uri": f"gs://{self.bucket.name}/{blob.name}"}


class HybridArtifactStore:
    def __init__(self, local_store: LocalArtifactStore, cloud_store: GCSArtifactStore | None = None) -> None:
        self.local_store = local_store
        self.cloud_store = cloud_store

    def save_png_b64(self, session_id: str, image_b64: str, filename: str) -> dict[str, str]:
        local = self.local_store.save_png_b64(session_id, image_b64, filename)
        if self.cloud_store is None:
            return local
        try:
            cloud = self.cloud_store.save_png_b64(session_id, image_b64, filename)
        except Exception:
            return local
        return {"path": local["path"], "uri": cloud["path"], "cloud_uri": cloud["path"]}

    def save_json(self, session_id: str, payload: dict, filename: str) -> dict[str, str]:
        local = self.local_store.save_json(session_id, payload, filename)
        if self.cloud_store is None:
            return local
        try:
            cloud = self.cloud_store.save_json(session_id, payload, filename)
        except Exception:
            return local
        return {"path": local["path"], "uri": cloud["path"], "cloud_uri": cloud["path"]}
