"""Local startup diagnostics. An owned OS lock is never deleted or stolen."""
from __future__ import annotations

import httpx

from video_translation.persistence.store import WorkerLock


def inspect_runtime(settings, port):
    lock = WorkerLock(settings.runtime / "worker.lock")
    try:
        lock.acquire()
    except RuntimeError:
        occupied = True
    else:
        occupied = False
        lock.release()
    health = None
    try:
        with httpx.Client(timeout=1, trust_env=False) as client:
            response = client.get(f"http://127.0.0.1:{port}/api/health")
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, dict) and "worker_running" in data:
                    health = data
    except (httpx.HTTPError, ValueError):
        pass
    return {"worker_occupied": occupied, "health": health, "url": f"http://127.0.0.1:{port}"}


def startup_message(state):
    if state["health"]:
        return (f"工作台已在运行：{state['url']}\n"
                "本次未启动第二个执行器。若刚更新代码，请先关闭原后台窗口（Ctrl+C），再运行启动命令。\n"
                "刷新网页只更新页面，不能替换仍在运行的Python后端。")
    if state["worker_occupied"]:
        return ("此运行目录仍由另一个后台执行器占用，但所选端口没有工作台响应。\n"
                "请核对原服务使用的端口和后台进程；不要删除worker.lock，也不要另起执行器。")
    return None
