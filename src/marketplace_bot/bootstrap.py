from marketplace_bot.config import SETTINGS
from marketplace_bot.state_store import StateStore


def bootstrap_runtime() -> None:
    StateStore(SETTINGS.runtime_dir).bootstrap()
