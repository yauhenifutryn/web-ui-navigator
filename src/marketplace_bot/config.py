from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True)
class Settings:
    cdp_url: str = os.getenv("MARKETPLACE_CDP_URL", "http://localhost:9222")
    target_domain: str = os.getenv("MARKETPLACE_TARGET_DOMAIN", "")
    runtime_dir: Path = Path(os.getenv("MARKETPLACE_RUNTIME_DIR", "runtime"))
    cloud_backend_url: str = os.getenv("NAVIGATOR_CLOUD_BACKEND_URL", "http://127.0.0.1:8080")
    use_cloud_backend: bool = os.getenv("NAVIGATOR_USE_CLOUD_BACKEND", "0").lower() in {"1", "true", "yes", "on"}
    gcp_project_id: str = os.getenv("GOOGLE_CLOUD_PROJECT", "")
    gcs_bucket: str = os.getenv("NAVIGATOR_GCS_BUCKET", "")
    state_file: str = "state.json"
    history_file: str = "history.json"
    ui_contract_file: str = "ui_contract.json"
    latest_scrape_file: str = "latest_scrape.txt"
    latest_decision_file: str = "latest_decision.json"
    error_dom_file: str = "error_dom.txt"
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-3-flash")
    gemini_index_model: str = os.getenv("GEMINI_INDEX_MODEL", "gemini-3-flash")
    gemini_live_model: str = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.1-flash-lite")
    ax_snapshots_enabled: bool = os.getenv("AX_SNAPSHOTS_ENABLED", "1").lower() in {"1", "true", "yes", "on"}
    ax_provider_preference: str = os.getenv("AX_PROVIDER_PREFERENCE", "mcp_then_cdp")
    ax_occlusion_mode: str = os.getenv("AX_OCCLUSION_MODE", "diagnostic")
    ax_max_nodes_index: int = int(os.getenv("AX_MAX_NODES_INDEX", "90"))
    ax_max_nodes_live: int = int(os.getenv("AX_MAX_NODES_LIVE", "48"))
    ax_max_nodes_verify: int = int(os.getenv("AX_MAX_NODES_VERIFY", "24"))

    @property
    def state_path(self) -> Path:
        return self.runtime_dir / self.state_file

    @property
    def history_path(self) -> Path:
        return self.runtime_dir / self.history_file

    @property
    def ui_contract_path(self) -> Path:
        return self.runtime_dir / self.ui_contract_file

    @property
    def latest_scrape_path(self) -> Path:
        return self.runtime_dir / self.latest_scrape_file

    @property
    def latest_decision_path(self) -> Path:
        return self.runtime_dir / self.latest_decision_file

    @property
    def error_dom_path(self) -> Path:
        return self.runtime_dir / self.error_dom_file


SETTINGS = Settings()
