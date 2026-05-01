"""
core.config — single source of truth for live config reload.

Extracted from agent.py in patch 21 to break the circular import that would
have happened if finder.py needed reload_config from agent.py.

`reload_config()` returns the latest config dict, re-reading from disk only
when the file mtime has changed (cheap to call every sweep).

`cfg` is a module-level convenience: it's loaded at import time so any
caller doing `from core.config import cfg` gets a working dict immediately.
For long-running processes, prefer to call `reload_config()` periodically
to pick up live edits.
"""

import os
import logging

import yaml

log = logging.getLogger("config")

_CFG_PATH  = "config.yaml"
_CFG_STATE = {"mtime": 0, "data": {}}


def reload_config():
    """Reload config.yaml if its mtime has changed. Idempotent; cheap to
    call every sweep."""
    try:
        mt = os.path.getmtime(_CFG_PATH)
    except OSError:
        return _CFG_STATE["data"]
    if mt != _CFG_STATE["mtime"]:
        with open(_CFG_PATH) as f:
            _CFG_STATE["data"]  = yaml.safe_load(f) or {}
        _CFG_STATE["mtime"] = mt
        if _CFG_STATE["mtime"] != 0:
            try:
                fs = _CFG_STATE["data"].get("min_fit_score")
                mu = _CFG_STATE["data"].get("max_urls_per_board")
                log.info(f"[CONFIG] reloaded (mtime={mt:.0f}, min_fit_score={fs}, max_urls_per_board={mu})")
            except Exception:
                pass
    return _CFG_STATE["data"]


# Initial load at import time so module-level `cfg` is usable immediately.
cfg = reload_config()
