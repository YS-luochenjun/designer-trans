from __future__ import annotations

import hashlib
import json
import shutil
import os
from pathlib import Path

from video_translation.agent_runtime.resources import ResourceCatalog
from video_translation.config import Settings
from video_translation.domain.models import TaskCreate
from video_translation.persistence.store import Store, identifier, now
from video_translation.workflow.graph import SUCCESS, instantiate, load_definition, task_status


class Conflict(ValueError):
    pass


class TaskService:
    def __init__(self, settings: Settings, store: Store):
        self.settings, self.store = settings, store
        self.definition = load_definition(settings.root)
        from .collaboration import Collaboration
        self.collaboration = Collaboration(self)
        for child in ("assets", "tasks", "deliveries", "uploads"):
            (settings.runtime / child).mkdir(parents=True, exist_ok=True)

    def create(self, request: TaskCreate, source="local") -> dict:
        for asset_id in request.asset_ids:
            self.store.require("asset", asset_id)
        definition = load_definition(self.settings.root, adaptive=True) if request.visual_route == "agent_adaptive" else self.definition
        task_id = identifier()
        timestamp = now()
        from video_translation.integrations.providers.model_catalog import resolve_selection
        model_snapshot = resolve_selection(self.settings.root)
        from video_translation.integrations.providers.separation import SeparationConfig
        separation_snapshot = SeparationConfig.from_env(self.settings.root).snapshot()
        from video_translation.execution.adaptive_image import image_snapshot
        from video_translation.integrations.providers.kie import KieConfig
        from video_translation.domain.execution import ExecutionProfile
        image_config_snapshot = image_snapshot(KieConfig.from_env() if os.environ.get("VT_MEDIA_PROVIDER") == "kie" else None)
        task = {
            **request.model_dump(),
            "id": task_id,
            "schema_version": "1.0.0",
            "source": source,
            "status": "queued",
            "created_at": timestamp,
            "updated_at": timestamp,
            "workflow": definition,
            "agent_engine": os.environ.get("VT_AGENT_ENGINE", "custom"),
            "media_provider": os.environ.get("VT_MEDIA_PROVIDER", "compatible"),
            "model_snapshot": model_snapshot,
            "separation_snapshot": separation_snapshot,
            "image_snapshot": image_config_snapshot,
            "resource_snapshot": ResourceCatalog(self.settings.root).snapshot(),
            "execution_snapshot": ExecutionProfile(mode=request.execution_profile).model_dump(),
            "nodes": instantiate(definition, request.asset_ids, request.target_locales),
            "revisions": {},
            "error": None,
        }
        if request.allow_review_draft:
            from .review_draft import make_authorization
            task["review_draft_authorization"] = make_authorization(task_id)
        self.store.put("task", task)
        return self.detail(task_id)

    def register_asset(self, temporary: Path, name: str) -> dict:
        asset_id = identifier()
        suffix = Path(name).suffix.lower()
        if suffix not in {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}:
            raise ValueError("仅支持MP4、MOV、MKV、WebM、AVI视频")
        target = self.settings.runtime / "assets" / f"{asset_id}{suffix}"
        shutil.move(str(temporary), str(target))
        record = {
            "id": asset_id,
            "name": Path(name).name,
            "path": str(target),
            "size_bytes": target.stat().st_size,
            "sha256": self.hash_file(target),
            "created_at": now(),
            "content_url": f"/api/assets/{asset_id}/content",
        }
        self.store.put("asset", record)
        return self.public_asset(record)

    @staticmethod
    def hash_file(path: Path) -> str:
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()

    @staticmethod
    def public_asset(record):
        return {key: value for key, value in record.items() if key != "path"}

    def checked_path(self, record: dict) -> Path:
        path = Path(record["path"]).resolve()
        if not path.is_relative_to(self.settings.runtime) or not path.is_file():
            raise KeyError("文件不存在或不属于运行目录")
        return path

    def artifact(self, task_id, node, path: Path, kind: str, name: str | None = None) -> dict:
        path = path.resolve()
        if not path.is_relative_to(self.settings.runtime):
            raise ValueError("产物必须位于运行目录")
        artifact_id = identifier()
        record = {
            "id": artifact_id,
            "task_id": task_id,
            "node_id": node["id"],
            "locale": node["locale"],
            "kind": kind,
            "name": name or path.name,
            "path": str(path),
            "sha256": self.hash_file(path),
            "size_bytes": path.stat().st_size,
            "created_at": now(),
            "content_url": f"/api/artifacts/{artifact_id}/content",
        }
        self.store.put("artifact", record)
        return record

    def node_directory(self, task_id, node):
        path = self.settings.runtime / "tasks" / task_id / "nodes" / node["id"] / str(node["attempt"])
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_json(self, task_id, node, name, data):
        path = self.node_directory(task_id, node) / name
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.artifact(task_id, node, path, "json")

    def summary(self, task):
        nodes = task["nodes"]
        return {
            key: task[key]
            for key in (
                "id",
                "title",
                "status",
                "source",
                "created_at",
                "updated_at",
                "target_locales",
                "asset_ids",
                "error",
            )
        } | {
            "progress": {"done": sum(n["status"] in SUCCESS for n in nodes), "total": len(nodes)},
            "current_nodes": [
                n["label"] + (f" · {n['locale']}" if n["locale"] else "")
                for n in nodes
                if n["status"] in {"running", "waiting_external", "waiting_user"}
            ],
            "delivery_count": sum(
                n["definition_id"] == "deliver" and n["status"] == "succeeded" for n in nodes
            ),
        }

    def detail(self, task_id):
        task = self.store.require("task", task_id)
        from .imagegen_bridge import public_requests
        from video_translation.execution.clean_progress import progress
        sessions = [r for r in self.store.list("production_session") if r["task_id"] == task_id]
        for session in sessions:
            owner = next((n for n in task["nodes"] if n["id"] == session["node_id"]), None)
            observed = next((n for n in task["nodes"] if owner and n["asset_id"] == owner["asset_id"]
                             and n["definition_id"] == "observe"), None)
            if observed and "texts" in (observed.get("output") or {}):
                texts = {item["id"]: {**item, **session.get("text_overrides", {}).get(item["id"], {})}
                         for item in observed["output"]["texts"] if item["treatment"] == "translate"}
                session["progress"] = progress(texts, session)
        return {
            **task,
            **self.summary(task),
            "assets": [self.public_asset(self.store.require("asset", value)) for value in task["asset_ids"]],
            "artifacts": [
                self.public_asset(r) for r in self.store.list("artifact") if r["task_id"] == task_id
            ],
            "questions": [r for r in self.store.list("question") if r["task_id"] == task_id],
            "deliveries": [r for r in self.store.list("delivery") if r["task_id"] == task_id],
            "operations": [r for r in self.store.list("operation") if r["task_id"] == task_id],
            "imagegen_requests": public_requests(self, task_id),
            "production_decisions": [r for r in self.store.list("production_decision") if r["task_id"] == task_id],
            "production_sessions": sessions,
            "agent_traces": sorted(
                [r for r in self.store.list("agent_trace") if r["task_id"] == task_id],
                key=lambda row: (row["created_at"], row["id"]),
            ),
            "conversations": self.collaboration.list(task_id),
            "route_autonomy": self.store.get("route_policy", task_id),
            "route_status": self.store.get("route_status", task_id),
        }

    def update_node(self, task_id, node_id, **changes):
        def mutate(task):
            node = next(n for n in task["nodes"] if n["id"] == node_id)
            # A cancelled task keeps any late outputs as evidence, but cannot advance.
            if task["status"] == "cancelled":
                changes.pop("status", None)
            node.update(changes)
            task["status"] = task_status(task)

        return self.store.update("task", task_id, mutate)

    def cancel(self, task_id):
        def mutate(task):
            if task["status"] == "completed":
                raise Conflict("已交付任务无需取消")
            task["status"] = "cancelled"
            for node in task["nodes"]:
                if node["status"] not in SUCCESS | {"failed", "unknown"}:
                    node["status"] = "cancelled"
                    node["finished_at"] = now()

        self.store.update("task", task_id, mutate)
        return self.detail(task_id)

    def retry(self, task_id):
        from video_translation.execution.remote import reconcile_kie_operations
        from .agent_recovery import replaced_with_authorization
        operations = reconcile_kie_operations(self, task_id)
        unresolved = {
            op["node_id"] for op in operations
            if op["status"] in {"unknown", "running"}
            and not replaced_with_authorization(self.store, op, "operation")
            and not (op.get("provider") == "kie" and op.get("resume_safe")
                     and (op.get("external_id") or op.get("recovery_mode") == "cached"))
        }
        unresolved.update(
            d["node_id"] for d in self.store.list("production_decision")
            if d.get("task_id") == task_id and d["status"] in {"unknown", "running", "invalid_response"}
            and not replaced_with_authorization(self.store, d, "production_decision"))
        def mutate(task):
            if task.get("control_paused"):
                raise Conflict("任务已暂停；重试不能解除暂停，请先明确恢复")
            if task["status"] not in {"blocked", "interrupted", "partial_completed"}:
                raise Conflict("只有阻塞、中断或部分完成的任务可以重试")
            protected = set(unresolved)
            unknown_nodes = [n for n in task["nodes"] if n["status"] == "unknown"]
            for unknown_node in unknown_nodes:
                pending = [op for op in operations if op["task_id"] == task_id and op["node_id"] == unknown_node["id"] and op["status"] in {"unknown", "running"}]
                if unknown_node["id"] in protected or not pending or any(op.get("provider") != "kie" or not op.get("resume_safe") or not (op.get("external_id") or op.get("recovery_mode") == "cached") for op in pending):
                    protected.add(unknown_node["id"])
                    continue
                unknown_node.update(status="interrupted", error="只查询原KIE任务继续，不重投")
            for node in task["nodes"]:
                if protected.intersection(node["depends_on"]):
                    protected.add(node["id"])
            changed = False
            rejected = [
                n
                for n in task["nodes"]
                if n["status"] == "failed"
                and n["definition_id"] == "review"
                and n["output"].get("human_rejected")
                and not any(item["id"] in protected for item in task["nodes"]
                            if item["asset_id"] == n["asset_id"] and item["locale"] == n["locale"])
            ]
            for review in rejected:
                branch = review["asset_id"] + ":" + review["locale"]
                count = task.setdefault("revisions", {}).get(branch, 0)
                if count >= task["max_revisions"]:
                    raise Conflict("该语言分支已达到返工上限；请核对原因后以新任务重新制定要求")
                task["revisions"][branch] = count + 1
                task.setdefault("revision_feedback", {})[branch] = [
                    {
                        "severity": "major",
                        "description": review["output"]["human_feedback"],
                        "source": "human",
                    }
                ]
                for item in task["nodes"]:
                    if item["asset_id"] == review["asset_id"] and item["locale"] == review["locale"]:
                        item.setdefault("history", []).append(
                            {
                                "attempt": item["attempt"],
                                "status": item["status"],
                                "output": item["output"],
                                "artifact_ids": item["artifact_ids"],
                            }
                        )
                        item.update(
                            status="pending", output={}, error=None, finished_at=None, artifact_ids=[]
                        )
                changed = True
            for node in task["nodes"]:
                if node["status"] in {"failed", "interrupted"} and node["id"] not in protected:
                    node.update(status="pending", error=None, finished_at=None)
                    changed = True
            if not changed:
                if protected:
                    raise Conflict("结果不明的节点及其下游保留，当前没有可独立重试的失败节点；请先核对原请求")
                raise Conflict("没有可重试的失败节点；待决问题需先回答")
            task["status"] = "queued"
            task["error"] = None

        self.store.update("task", task_id, mutate)
        return self.detail(task_id)

    def set_external_limits(self, task_id, limits):
        def mutate(task):
            if task["status"] in {"completed", "cancelled"}:
                raise Conflict("已结束的任务不再修改外部调用设置")
            fields = limits.model_dump(exclude_unset=True)
            previous = {key: task.get(key) for key in fields}
            if previous != fields:
                task.setdefault("limit_changes", []).append({
                    "at": now(), "before": previous, "after": fields,
                    "paid_calls": task.get("paid_calls", 0),
                    "image_calls": task.get("image_calls", 0),
                    "video_credits_committed": task.get("video_credits_committed", 0),
                })
                task.update(fields)
        self.store.update("task", task_id, mutate)
        return self.detail(task_id)

    def question(self, task_id, node, prompt, kind, options, context=None):
        record = {
            "id": identifier(),
            "task_id": task_id,
            "node_id": node["id"],
            "prompt": prompt,
            "kind": kind,
            "options": options,
            "context": context or {},
            "attempt": node["attempt"],
            "status": "open",
            "answer": None,
            "created_at": now(),
        }
        self.store.put("question", record)
        return record

    def answer(self, task_id, question_id, answer, feedback=""):
        with self.store.connection() as db:

            def read(kind, key):
                row = db.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
                if row is None:
                    raise KeyError(key)
                return json.loads(row[0])

            question = read("question", question_id)
            if question["task_id"] != task_id or question["status"] != "open":
                raise Conflict("问题不属于任务或已回答")
            task = read("task", task_id)
            if task["status"] == "cancelled":
                raise Conflict("任务已取消")
            node = next(n for n in task["nodes"] if n["id"] == question["node_id"])
            if node["status"] != "waiting_user" or node["attempt"] != question.get(
                "attempt", node["attempt"]
            ):
                raise Conflict("问题对应节点或尝试已变更")
            if question["kind"] == "review":
                if answer not in {"approve", "reject"}:
                    raise ValueError("审核回答必须是approve或reject")
                artifact = read("artifact", question["context"]["artifact_id"])
                if self.hash_file(self.checked_path(artifact)) != question["context"]["sha256"]:
                    raise Conflict("候选内容已变化，旧问题不能批准新版本")
                output = dict(node["output"])
                if answer == "approve":
                    output.update(quality_status="human_approved", approved_sha256=artifact["sha256"])
                    node.update(status="succeeded", output=output, finished_at=now())
                else:
                    node.update(
                        status="failed",
                        error="人工审核未通过；填写的意见将在重试时用于返工",
                        output={
                            **node["output"],
                            "human_rejected": True,
                            "human_feedback": feedback
                            or "人工完整视听审核未通过，请重新核查译文、声音和画面",
                        },
                        finished_at=now(),
                    )
            elif question["kind"] == "brief":
                # The observed inventory remains authoritative; confirmation is not a new inference.
                node.update(status="succeeded", output={**node["output"], "brief_confirmed": True},
                            error=None, finished_at=now())
                task.setdefault("confirmed_briefs", {})[node["asset_id"]] = answer
                task["brief_revision"] = task.get("brief_revision", 0) + 1
                node["output"].pop("_wait", None)
            else:
                node.update(status="pending", output={**node["output"], "user_answer": answer}, error=None)
            question.update(status="answered", answer=answer, feedback=feedback, answered_at=now())
            task.update(status=task_status(task), updated_at=now())
            for kind, value in (("question", question), ("task", task)):
                db.execute(
                    "UPDATE records SET data=? WHERE kind=? AND id=?",
                    (json.dumps(value, ensure_ascii=False), kind, value["id"]),
                )
        return self.detail(task_id)

    def commit_delivery(self, task_id, node_id, delivery, digest):
        # Serialize final approval, delivery registration and completion against cancellation.
        with self.store.connection() as db:
            row = db.execute("SELECT data FROM records WHERE kind='task' AND id=?", (task_id,)).fetchone()
            task = json.loads(row[0])
            if task["status"] == "cancelled":
                raise Conflict("任务已取消，候选副本保留但不登记交付")
            node = next(n for n in task["nodes"] if n["id"] == node_id)
            review = next(
                n
                for n in task["nodes"]
                if n["definition_id"] == "review"
                and n["asset_id"] == node["asset_id"]
                and n["locale"] == node["locale"]
            )
            if review["status"] != "succeeded" or review["output"].get("approved_sha256") != digest:
                raise Conflict("当前审核版本不能放行交付")
            node.update(status="succeeded", output=delivery, finished_at=now())
            task["status"] = task_status(task)
            task["updated_at"] = now()
            db.execute(
                "INSERT INTO records VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data",
                (
                    "delivery",
                    delivery["id"],
                    json.dumps(delivery, ensure_ascii=False),
                    delivery["created_at"],
                ),
            )
            db.execute(
                "UPDATE records SET data=? WHERE kind='task' AND id=?",
                (json.dumps(task, ensure_ascii=False), task_id),
            )

    def recover(self):
        from .agent_recovery import replaced_with_authorization
        self.collaboration.recover()
        for record in self.store.list("operation"):
            if record["status"] == "running":
                self.store.update(
                    "operation",
                    record["id"],
                    lambda op: op.update(status="unknown", error="进程中断，外部结果待核对"),
                )
        for task in self.store.list("task"):
            unknown = {
                op["node_id"]
                for op in self.store.list("operation")
                if op["task_id"] == task["id"] and op["status"] == "unknown"
                and not replaced_with_authorization(self.store, op, "operation")
            }

            def mutate(current):
                if current["status"] in {"completed", "cancelled"}:
                    return
                for node in current["nodes"]:
                    if node["status"] == "waiting_external" and node.get("tool_request_id"):
                        # Host tool receipts are independent of KIE polling and recovery.
                        continue
                    if (node["status"] == "waiting_external" and node.get("external_pending")
                            and node.get("next_poll_at")):
                        # The worker reconciles the original journal before polling.
                        continue
                    if node["status"] in {"running", "waiting_external"}:
                        node.update(
                            status="unknown" if node["id"] in unknown else "interrupted",
                            error="服务重启，保留现场",
                        )
                from video_translation.execution.stage_recovery import reconcile_stage_outputs
                reconcile_stage_outputs(recovery_service, current)
                current["status"] = task_status(current)

            from types import SimpleNamespace
            from video_translation.persistence.transaction import TransactionStore
            with self.store.connection() as db:
                tx = TransactionStore(self.store, db)
                recovery_service = SimpleNamespace(
                    store=tx, settings=self.settings, hash_file=self.hash_file)
                tx.update("task", task["id"], mutate)
