"""Shared state-file IO for the MCP server.

Both `auth.py` (pending device flow) and `tools.py` (active thread)
persist to ~/.config/remotecodetrol/state.json. Keeping the read/write
helpers in one place avoids circular imports and lets both modules
preserve each other's keys when updating.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


STATE_PATH = Path(os.path.expanduser("~/.config/remotecodetrol/state.json"))


def read_state() -> dict[str, Any]:
    """Read state.json; return {} if missing or corrupt."""
    try:
        return json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_state(state: dict[str, Any]) -> None:
    """Write state.json (creates parent dirs)."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def update_state(updates: dict[str, Any]) -> dict[str, Any]:
    """Merge `updates` into state.json. Pass `{key: None}` to delete a key."""
    state = read_state()
    for k, v in updates.items():
        if v is None:
            state.pop(k, None)
        else:
            state[k] = v
    write_state(state)
    return state
