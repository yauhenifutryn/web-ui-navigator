from __future__ import annotations

from dataclasses import dataclass

from marketplace_bot.artifact_store import GCSArtifactStore, HybridArtifactStore, LocalArtifactStore
from marketplace_bot.bridge import LocalBrowserBridge
from marketplace_bot.companion import LiveNavigatorCompanion
from marketplace_bot.config import SETTINGS
from marketplace_bot.goal_compiler import GoalCompiler
from marketplace_bot.llm.factory import build_llm_client
from marketplace_bot.planner import PlannerService
from marketplace_bot.session_repository import FirestoreSessionRepository, HybridSessionRepository, LocalJsonSessionRepository
from marketplace_bot.site_memory_repository import (
    FirestoreSiteMemoryRepository,
    HybridSiteMemoryRepository,
    LocalJsonSiteMemoryRepository,
)
from marketplace_bot.state_store import StateStore


@dataclass
class NavigatorRuntime:
    state_store: StateStore
    session_repository: HybridSessionRepository
    site_memory_repository: HybridSiteMemoryRepository
    artifact_store: HybridArtifactStore
    companion: LiveNavigatorCompanion
    bridge: LocalBrowserBridge


def build_navigator_runtime(state_store: StateStore | None = None) -> NavigatorRuntime:
    store = state_store or StateStore(SETTINGS.runtime_dir)
    store.bootstrap()

    local_sessions = LocalJsonSessionRepository(store.runtime_dir)
    cloud_sessions = None
    if SETTINGS.gcp_project_id:
        try:
            cloud_sessions = FirestoreSessionRepository()
        except Exception:
            cloud_sessions = None
    session_repository = HybridSessionRepository(local_sessions, cloud_sessions)

    local_site_memory = LocalJsonSiteMemoryRepository(store.runtime_dir)
    cloud_site_memory = None
    if SETTINGS.gcp_project_id:
        try:
            cloud_site_memory = FirestoreSiteMemoryRepository()
        except Exception:
            cloud_site_memory = None
    site_memory_repository = HybridSiteMemoryRepository(local_site_memory, cloud_site_memory)

    local_artifacts = LocalArtifactStore(store.runtime_dir)
    cloud_artifacts = None
    if SETTINGS.gcs_bucket:
        try:
            cloud_artifacts = GCSArtifactStore(SETTINGS.gcs_bucket)
        except Exception:
            cloud_artifacts = None
    artifact_store = HybridArtifactStore(local_artifacts, cloud_artifacts)

    llm_client = build_llm_client()
    companion = LiveNavigatorCompanion(
        session_repository=session_repository,
        site_memory_repository=site_memory_repository,
        goal_compiler=GoalCompiler(),
        planner=PlannerService(llm_client=llm_client),
    )
    bridge = LocalBrowserBridge(
        state_store=store,
        cdp_url=SETTINGS.cdp_url,
        target_domain=SETTINGS.target_domain,
        artifact_store=artifact_store,
        site_memory_repository=site_memory_repository,
    )

    return NavigatorRuntime(
        state_store=store,
        session_repository=session_repository,
        site_memory_repository=site_memory_repository,
        artifact_store=artifact_store,
        companion=companion,
        bridge=bridge,
    )
