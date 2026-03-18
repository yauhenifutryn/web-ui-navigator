from __future__ import annotations

import json
from pathlib import Path

from marketplace_bot.navigator_models import SiteMemory

try:
    from google.cloud import firestore
except Exception:  # pragma: no cover
    firestore = None


class LocalJsonSiteMemoryRepository:
    def __init__(self, runtime_dir: Path) -> None:
        self.base_dir = runtime_dir / "site_memory"
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save(self, memory: SiteMemory) -> SiteMemory:
        path = self.base_dir / f"{memory.memory_key}.json"
        path.write_text(memory.model_dump_json(indent=2), encoding="utf-8")
        return memory

    def get(self, memory_key: str) -> SiteMemory | None:
        path = self.base_dir / f"{memory_key}.json"
        if not path.exists():
            return None
        return SiteMemory.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def get_by_origin(self, domain_pack: str, site_origin: str) -> SiteMemory | None:
        for path in sorted(self.base_dir.glob("*.json")):
            try:
                memory = SiteMemory.model_validate(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
            if memory.domain_pack == domain_pack and memory.site_origin == site_origin:
                return memory
        return None


class FirestoreSiteMemoryRepository:
    def __init__(self, collection_name: str = "navigator_site_memory") -> None:
        if firestore is None:
            raise RuntimeError("google-cloud-firestore is not installed")
        self.client = firestore.Client()
        self.collection = self.client.collection(collection_name)

    def save(self, memory: SiteMemory) -> SiteMemory:
        self.collection.document(memory.memory_key).set(memory.model_dump(mode="json"))
        return memory

    def get(self, memory_key: str) -> SiteMemory | None:
        snapshot = self.collection.document(memory_key).get()
        if not snapshot.exists:
            return None
        return SiteMemory.model_validate(snapshot.to_dict() or {})

    def get_by_origin(self, domain_pack: str, site_origin: str) -> SiteMemory | None:
        rows = (
            self.collection.where("domain_pack", "==", domain_pack)
            .where("site_origin", "==", site_origin)
            .limit(1)
            .stream()
        )
        for row in rows:
            return SiteMemory.model_validate(row.to_dict() or {})
        return None


class HybridSiteMemoryRepository:
    def __init__(
        self,
        local_repo: LocalJsonSiteMemoryRepository,
        cloud_repo: FirestoreSiteMemoryRepository | None = None,
    ) -> None:
        self.local_repo = local_repo
        self.cloud_repo = cloud_repo

    def save(self, memory: SiteMemory) -> SiteMemory:
        self.local_repo.save(memory)
        if self.cloud_repo is not None:
            try:
                self.cloud_repo.save(memory)
            except Exception:
                pass
        return memory

    def get(self, memory_key: str) -> SiteMemory | None:
        local = self.local_repo.get(memory_key)
        if local is not None:
            return local
        if self.cloud_repo is not None:
            try:
                cloud = self.cloud_repo.get(memory_key)
            except Exception:
                cloud = None
            if cloud is not None:
                self.local_repo.save(cloud)
            return cloud
        return None

    def get_by_origin(self, domain_pack: str, site_origin: str) -> SiteMemory | None:
        local = self.local_repo.get_by_origin(domain_pack, site_origin)
        if local is not None:
            return local
        if self.cloud_repo is not None:
            try:
                cloud = self.cloud_repo.get_by_origin(domain_pack, site_origin)
            except Exception:
                cloud = None
            if cloud is not None:
                self.local_repo.save(cloud)
            return cloud
        return None
