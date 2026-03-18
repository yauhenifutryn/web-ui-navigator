from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class StateStore:
    def __init__(self, runtime_dir: Path) -> None:
        self.runtime_dir = runtime_dir

    @property
    def state_path(self) -> Path:
        return self.runtime_dir / "state.json"

    @property
    def history_path(self) -> Path:
        return self.runtime_dir / "history.json"

    @property
    def ui_contract_path(self) -> Path:
        return self.runtime_dir / "ui_contract.json"

    @property
    def latest_scrape_path(self) -> Path:
        return self.runtime_dir / "latest_scrape.txt"

    @property
    def latest_decision_path(self) -> Path:
        return self.runtime_dir / "latest_decision.json"

    @property
    def error_dom_path(self) -> Path:
        return self.runtime_dir / "error_dom.txt"

    def bootstrap(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._write_json_if_missing(
            self.state_path,
            {
                "meta": {"status": "WAITING", "last_updated": None},
                "quarter": {"label": None},
                "data": {},
                "errors": [],
            },
        )
        self._write_json_if_missing(self.history_path, {"entries": []})
        self._write_json_if_missing(
            self.ui_contract_path,
            {
                "version": "v1",
                "selector_strategy": "semantic_locators_only",
                "updated_at": None,
            },
        )
        self._write_text_if_missing(self.latest_scrape_path, "")
        self._write_json_if_missing(
            self.latest_decision_path,
            {
                "captured_at": None,
                "mode": None,
                "raw_output": "",
                "decisions": [],
            },
        )
        self._write_text_if_missing(self.error_dom_path, "")

    def _atomic_write(self, path: Path, content: str) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", delete=False, dir=self.runtime_dir, encoding="utf-8") as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        tmp_path.replace(path)

    def _write_json_if_missing(self, path: Path, payload: dict[str, Any]) -> None:
        if not path.exists():
            self._atomic_write(path, json.dumps(payload, indent=2, ensure_ascii=False))

    def _write_text_if_missing(self, path: Path, content: str) -> None:
        if not path.exists():
            self._atomic_write(path, content)

    def write_json(self, path: Path, payload: dict[str, Any]) -> None:
        self._atomic_write(path, json.dumps(payload, indent=2, ensure_ascii=False))

    def read_json(self, path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
        if not path.exists():
            return {} if default is None else default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {} if default is None else default

    def write_text(self, path: Path, content: str) -> None:
        self._atomic_write(path, content)

    def read_text(self, path: Path, default: str = "") -> str:
        if not path.exists():
            return default
        return path.read_text(encoding="utf-8")

    def append_history(self, entry: dict[str, Any]) -> None:
        history = self.read_json(self.history_path, default={"entries": []})
        entries = history.get("entries", [])
        if not isinstance(entries, list):
            entries = []
        entries.append(entry)
        history["entries"] = entries
        self.write_json(self.history_path, history)

    def write_latest_scrape(self, content: str) -> None:
        self.write_text(self.latest_scrape_path, content)

    def write_latest_decision(self, payload: dict[str, Any]) -> None:
        if "captured_at" not in payload:
            payload["captured_at"] = utc_now_iso()
        self.write_json(self.latest_decision_path, payload)
