from __future__ import annotations
import json
from app.models import StoreEvent


def emit_event(event_dict: dict, output_path: str) -> bool:
    try:
        StoreEvent.model_validate(event_dict)
    except Exception as e:
        print(f"[emit] Invalid event skipped: {e}")
        return False
    with open(output_path, "a") as f:
        f.write(json.dumps(event_dict) + "\n")
    return True


def emit_batch(events: list[dict], output_path: str) -> dict:
    accepted, errors = 0, []
    for e in events:
        if emit_event(e, output_path):
            accepted += 1
        else:
            errors.append(e.get("event_id", "unknown"))
    return {"accepted": accepted, "errors": errors}