from pathlib import Path

from marketplace_bot.config import SETTINGS
from marketplace_bot.state_store import StateStore


def main() -> None:
    store = StateStore(SETTINGS.runtime_dir)
    store.runtime_dir.mkdir(parents=True, exist_ok=True)

    sessions_dir = store.runtime_dir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    for session_file in sessions_dir.glob("*.json"):
        session_file.unlink()

    for name in ["history.json", "latest_decision.json", "latest_scrape.txt", "error_dom.txt", "state.json"]:
        target = store.runtime_dir / name
        if target.exists():
            target.unlink()

    store.bootstrap()

    remaining = sorted(path.name for path in sessions_dir.glob("*.json"))
    print(f"Reset runtime cache in {store.runtime_dir}.")
    print(f"Remaining session files: {remaining}.")


if __name__ == "__main__":
    main()
