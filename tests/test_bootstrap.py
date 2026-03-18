import json

from marketplace_bot import bootstrap


def test_bootstrap_creates_runtime_files(tmp_path, monkeypatch):
    class FakeSettings:
        runtime_dir = tmp_path / "runtime"
        state_path = runtime_dir / "state.json"
        history_path = runtime_dir / "history.json"
        ui_contract_path = runtime_dir / "ui_contract.json"
        latest_scrape_path = runtime_dir / "latest_scrape.txt"
        latest_decision_path = runtime_dir / "latest_decision.json"
        error_dom_path = runtime_dir / "error_dom.txt"

    monkeypatch.setattr(bootstrap, "SETTINGS", FakeSettings)

    bootstrap.bootstrap_runtime()

    assert FakeSettings.runtime_dir.exists()
    assert json.loads(FakeSettings.state_path.read_text(encoding="utf-8"))["meta"]["status"] == "WAITING"
    assert json.loads(FakeSettings.history_path.read_text(encoding="utf-8"))["entries"] == []
    assert json.loads(FakeSettings.ui_contract_path.read_text(encoding="utf-8"))["version"] == "v1"
    assert FakeSettings.latest_scrape_path.exists()
    assert json.loads(FakeSettings.latest_decision_path.read_text(encoding="utf-8"))["raw_output"] == ""
    assert FakeSettings.error_dom_path.exists()
