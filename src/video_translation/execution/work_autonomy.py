"""Runtime opt-in for autonomous method selection, independent of cost limits."""
import os


def enabled(task):
    return (task.get("agent_engine") == "codex"
            and os.environ.get("VT_CODEX_WORK_AUTONOMY") == "open")
