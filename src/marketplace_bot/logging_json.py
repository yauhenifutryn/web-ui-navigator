import json
from datetime import datetime, timezone
from typing import Any


def log_event(agent: str, event: str, **fields: Any) -> None:
    payload = {
        "ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "agent": agent,
        "event": event,
        **fields,
    }
    print(json.dumps(payload, ensure_ascii=False))
