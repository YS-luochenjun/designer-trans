"""Reviewed local task deletion with worker exclusion and recoverable quarantine.

The optional quiescence(task_id) context manager must hold the worker dispatch
mutex, reject unfinished futures, and prevent new dispatch for the entire call.
Without it, the OS worker lock must be available. Cancellation is not quiescence.
Files move into same-volume quarantine before database commit; retrying execute
with the original fingerprint recovers interruptions without external requests.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from video_translation.application.tasks import Conflict
from video_translation.persistence.store import WorkerLock, now
from video_translation.workflow.graph import ready_nodes


class DeletionConflict(Conflict):
    """The reviewed deletion is stale, unsafe, or still running."""


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _save(db, kind, record):
    db.execute("INSERT INTO records VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data",
               (kind, record["id"], json.dumps(record, ensure_ascii=False), record.get("created_at", now())))


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


class TaskDeletionService:
    def __init__(self, settings, store, *, quiescence=None):
        self.settings, self.store = settings, store
        # Resolving first would erase evidence of a linked runtime.
        self.runtime = Path(os.path.abspath(settings.runtime))
        self.quiescence = quiescence

    @staticmethod
    def _identifier(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            raise DeletionConflict("任务或素材标识不是安全的本地路径标识")
        return value

    def _check_path(self, path):
        path = Path(os.path.abspath(path))
        if not path.is_relative_to(self.runtime) or path == self.runtime:
            raise DeletionConflict("文件不属于本地任务管理目录")
        for part in [*reversed(path.parents), path]:
            try:
                info = part.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise DeletionConflict("删除路径包含符号链接或重解析点，已拒绝")
        return path

    def _inventory(self, root):
        root = self._check_path(root)
        result = []
        if not root.exists():
            return result
        pending = [root]
        while pending:
            path = pending.pop()
            self._check_path(path)
            info = path.lstat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise DeletionConflict("删除范围包含非常规文件")
            result.append({"path": path.relative_to(self.runtime).as_posix(),
                           "directory": stat.S_ISDIR(info.st_mode), "size_bytes": info.st_size,
                           "mtime_ns": info.st_mtime_ns, "inode": info.st_ino,
                           "device": info.st_dev})
            if stat.S_ISREG(info.st_mode):
                with path.open("rb") as handle:
                    result[-1]["sha256"] = hashlib.file_digest(handle, "sha256").hexdigest()
            if stat.S_ISDIR(info.st_mode):
                pending.extend(sorted(path.iterdir(), reverse=True))
        return sorted(result, key=lambda item: item["path"])

    @contextmanager
    def _quiet(self, task_id):
        self._check_path(self.runtime / "worker.lock")
        if self.quiescence is not None:
            with self.quiescence(task_id):
                yield
            return
        guard = WorkerLock(self.runtime / "worker.lock")
        try:
            guard.acquire()
        except RuntimeError as exc:
            raise DeletionConflict("后台执行器仍运行；请先取消任务并等待执行结束") from exc
        try:
            yield
        finally:
            guard.release()

    def preview(self, task_id):
        self._identifier(task_id)
        with self.store.connection() as db:
            plan = self._plan(db, task_id)
        try:
            with self._quiet(task_id):
                pass
        except Conflict as exc:
            plan["blockers"].append(str(exc))
        plan["can_delete"] = not plan["blockers"]
        return {key: value for key, value in plan.items() if not key.startswith("_")}

    def _completed_response(self, operation, task):
        """A received synchronous response is terminal even if parsing failed."""
        if (operation.get("provider") != "kie" or operation.get("status") != "failed"
                or operation.get("kind") != "audio_understanding"
                or operation.get("external_id")):
            return None
        from video_translation.execution.remote import _inspect_journal, _journal_location
        try:
            _, path = _journal_location(self, operation)
            self._check_path(path)
            observed = _inspect_journal(path, operation, task)
            journal = json.loads(path.read_bytes())
            choices = journal.get("response", {}).get("choices")
            if (observed["journal_state"] != "response-invalid"
                    or not isinstance(choices, list) or not choices
                    or not all(isinstance(choice, dict) and choice.get("finish_reason")
                               for choice in choices)):
                return None
            return {"operation": operation, "journal": journal, "verification": observed}
        except (OSError, ValueError, TypeError, AttributeError, Conflict):
            return None

    def _plan(self, db, task_id):
        rows = {(kind, key): json.loads(data) for kind, key, data in
                db.execute("SELECT kind,id,data FROM records ORDER BY kind,id")}
        task = rows.get(("task", task_id))
        if task is None:
            raise KeyError(f"task: {task_id}")
        blockers = []
        if task.get("status") in {"queued", "running", "waiting_external"}:
            blockers.append("任务仍在排队或运行，请先取消并等待执行结束")
        if task.get("status") != "cancelled" and ready_nodes(task):
            blockers.append("任务仍有可执行节点，请先取消")
        if any(n.get("status") in {"running", "waiting_external", "unknown"} for n in task.get("nodes", [])):
            blockers.append("存在活跃或结果不明的节点，需先停稳或对账")
        owned = {("task", task_id)}
        node_ids = {n["id"] for n in task.get("nodes", [])}
        for key, record in rows.items():
            if key[0] in {"task", "asset", "requirement", "schedule", "deletion_receipt"}:
                continue
            if record.get("task_id") == task_id or (
                record.get("task_id") is None and record.get("node_id") in node_ids
            ):
                owned.add(key)
        changed = True
        while changed:
            changed = False
            owned_ids = {key for _, key in owned}
            for key, record in rows.items():
                if key in owned or key[0] in {"task", "asset", "requirement", "schedule", "deletion_receipt"}:
                    continue
                if record.get("task_id") not in (None, task_id):
                    continue
                if any(record.get(field) in owned_ids for field in
                       ("submission_id", "operation_id", "candidate_id", "delivery_id")):
                    owned.add(key)
                    changed = True
        updates = {}
        for key, record in rows.items():
            if key[0] == "requirement" and task_id in record.get("task_ids", []):
                remaining = [value for value in record["task_ids"] if value != task_id]
                if not remaining:
                    owned.add(key)
                else:
                    updates[key] = {**record, "task_ids": remaining}
            elif key[0] == "schedule" and record.get("last_task_id") == task_id:
                if record.get("running"):
                    blockers.append("关联计划仍在触发，请等待结束")
                updates[key] = {**record, "last_task_id": None}
        external_receipts = []
        for kind, key in sorted(owned):
            record = rows[(kind, key)]
            completed_response = self._completed_response(record, task) if kind == "operation" else None
            if completed_response:
                external_receipts.append(completed_response)
            # New journal kinds are owned dynamically, so uncertain results must
            # also be checked independently of the legacy operation kind.
            uncertain = {"unknown", "running", "needs_reconciliation", "submitted",
                         "sending", "dispatched", "waiting_external"}
            states = {value for field in ("status", "state")
                      if isinstance(value := record.get(field), str)}
            paid_journal = kind in {
                "production_decision", "production_image", "observation_decision",
                "adaptive_audio_segment", "adaptive_speech_revision",
            }
            if states & uncertain or (paid_journal and states & {"pending", "reserved"}):
                blockers.append("存在未收口的执行或付费回执，需先核对并保留恢复证据")
            if kind == "operation" and (
                record.get("status") not in {"succeeded", "done", "failed", "cancelled"}
                or (record.get("provider") == "kie" and record.get("status") not in {"succeeded", "done"}
                    and record.get("journal_state") not in {"success", "fail"} and not completed_response)
            ):
                blockers.append("存在未对账的外部操作，需保留恢复证据")
            if kind in {"outbox", "requirement_submission"}:
                states = [record.get("state", record.get("status"))]
                states += [op.get("state", op.get("status")) for op in record.get("operations", [])]
                if any(value in {"unknown", "running", "pending", "sending", "dispatched", "needs_reconciliation"}
                       for value in states):
                    blockers.append("外部提交尚未确认，需保留回执和恢复证据")
        shared = []
        preserved = []
        roots = [self.runtime / "tasks" / task_id, self.runtime / "deliveries" / task_id]
        for asset_id in sorted(set(task.get("asset_ids", []))):
            self._identifier(asset_id)
            asset_key = ("asset", asset_id)
            asset = rows.get(asset_key)
            if asset is None:
                continue
            references = [f"{kind}:{key}" for (kind, key), record in rows.items()
                          if (kind, key) not in owned and (kind, key) != asset_key
                          and kind != "deletion_receipt" and asset_id in set(_strings(updates.get((kind, key), record)))]
            if references:
                shared.append({"id": asset_id, "references": references})
                continue
            path = Path(os.path.abspath(asset.get("path", "")))
            owned.add(asset_key)
            if path.parent == self.runtime / "assets" and path.stem == asset_id:
                roots.append(path)
            else:
                preserved.append({"id": asset_id, "reason": "external_or_unowned_input"})
        for key, record in rows.items():
            if key in owned or key[0] == "deletion_receipt":
                continue
            for value in _strings(updates.get(key, record)):
                if not Path(value).is_absolute():
                    continue
                candidate = Path(os.path.abspath(value))
                if any(candidate == root or candidate.is_relative_to(root) for root in roots):
                    blockers.append("其他记录仍引用任务目录内文件，需先处理共享引用")
                    break
        for key in sorted(owned):
            record = rows[key]
            if key[0] in {"artifact", "audio_segment", "candidate", "delivery"} and record.get("path"):
                path = Path(os.path.abspath(record["path"]))
                if not any(path == root or path.is_relative_to(root) for root in roots):
                    preserved.append({"id": key[1], "reason": "external_or_unowned_output"})
        entries = []
        for root in roots:
            try:
                entries.extend(self._inventory(root))
            except (OSError, Conflict) as exc:
                blockers.append(str(exc))
        files = [entry for entry in entries if not entry["directory"]]
        identity = {"task_id": task_id, "records": [[*key, rows[key]] for key in sorted(owned)],
                    "updates": [[*key, rows[key], value] for key, value in sorted(updates.items())],
                    "shared_assets": shared, "entries": entries, "preserved_files": preserved,
                    "external_receipts": external_receipts}
        receipt = rows.get(("deletion_receipt", task_id))
        if receipt and receipt.get("state") == "prepared":
            blockers.append("上次删除中断，请使用原预览指纹重试以恢复现场")
        return {"task_id": task_id, "fingerprint": _digest(identity), "can_delete": not blockers,
                "retained_external_receipts": len(external_receipts), "_external_receipts": external_receipts,
                "blockers": list(dict.fromkeys(blockers)), "files": files,
                "file_count": len(files), "bytes": sum(item["size_bytes"] for item in files),
                "shared_assets": shared, "preserved_files": preserved,
                "records": dict(Counter(kind for kind, _ in owned)),
                "includes_final_outputs": True, "external_platform_affected": False,
                "_owned": sorted(owned), "_updates": updates, "_entries": entries,
                "_roots": [path.relative_to(self.runtime).as_posix() for path in roots if path.exists()]}

    def _stage_paths(self, receipt):
        base = self.runtime / ".task-deletions" / self._identifier(receipt["id"])
        for index, relative in enumerate(receipt["roots"]):
            source = self._check_path(self.runtime / relative)
            parts = source.relative_to(self.runtime).parts
            valid = (len(parts) == 2 and parts[0] in {"tasks", "deliveries"} and parts[1] == receipt["id"])
            valid |= (len(parts) == 2 and parts[0] == "assets" and source.stem in receipt["asset_ids"])
            if not valid:
                raise DeletionConflict("删除恢复清单含不属于该任务的路径")
            yield source, self._check_path(base / str(index))

    def _restore(self, receipt):
        for source, staged in reversed(list(self._stage_paths(receipt))):
            if staged.exists():
                self._inventory(staged)
                if source.exists():
                    raise DeletionConflict("删除恢复位置已被占用，保留隔离文件等待核对")
                staged.rename(source)

    def _purge(self, receipt):
        expected = {item["path"]: item for item in receipt["entries"]}
        for source, staged in self._stage_paths(receipt):
            entries = self._inventory(staged)
            for item in entries:
                path = self.runtime / item["path"]
                original = (source / path.relative_to(staged)).relative_to(self.runtime).as_posix()
                reviewed = expected.get(original)
                if reviewed is None or reviewed["directory"] != item["directory"]:
                    raise DeletionConflict("隔离目录出现未预览文件，已保留等待核对")
                if not item["directory"] and any(item[field] != reviewed[field] for field in
                                                  ("sha256", "size_bytes", "inode", "device")):
                    raise DeletionConflict("隔离文件已变化，已保留等待核对")
            for item in sorted(entries, key=lambda value: len(Path(value["path"]).parts), reverse=True):
                path = self._check_path(self.runtime / item["path"])
                if item["directory"]:
                    path.rmdir()
                else:
                    path.unlink()
        base = self._check_path(self.runtime / ".task-deletions" / receipt["id"])
        if base.exists():
            base.rmdir()

    def execute(self, task_id, fingerprint):
        self._identifier(task_id)
        if not isinstance(fingerprint, str) or not re.fullmatch("[0-9a-f]{64}", fingerprint):
            raise DeletionConflict("请提供已确认的删除预览指纹")
        with self._quiet(task_id):
            receipt = self.store.get("deletion_receipt", task_id)
            if receipt:
                if receipt["fingerprint"] != fingerprint:
                    raise DeletionConflict("删除预览已变化，请重新预览")
                if receipt["state"] == "prepared":
                    self._restore(receipt)
                    self.store.delete("deletion_receipt", task_id)
                elif receipt["state"] == "completed":
                    return self._result(receipt, False)
                else:
                    return self._finish(receipt)
            with self.store.connection() as db:
                plan = self._plan(db, task_id)
                if plan["fingerprint"] != fingerprint:
                    raise DeletionConflict("删除预览已变化，请重新预览")
                if plan["blockers"]:
                    raise DeletionConflict("；".join(plan["blockers"]))
                receipt = {"id": task_id, "fingerprint": fingerprint, "state": "prepared",
                           "roots": plan["_roots"], "asset_ids": [key for kind, key in plan["_owned"] if kind == "asset"],
                           "file_count": plan["file_count"], "bytes": plan["bytes"],
                           "records": plan["records"], "entries": plan["_entries"],
                           "external_receipts": plan["_external_receipts"], "created_at": now()}
                _save(db, "deletion_receipt", receipt)
            try:
                with self.store.connection() as db:
                    current = self._plan(db, task_id)
                    if current["fingerprint"] != fingerprint:
                        raise DeletionConflict("删除预览已变化，请重新预览")
                    # All blockers except our own prepared journal remain fatal.
                    blockers = [item for item in current["blockers"] if not item.startswith("上次删除中断")]
                    if blockers:
                        raise DeletionConflict("；".join(blockers))
                    for source, staged in self._stage_paths(receipt):
                        staged.parent.mkdir(parents=True, exist_ok=True)
                        if staged.exists():
                            raise DeletionConflict("删除隔离位置已存在，需先核对恢复现场")
                        source.rename(staged)
                        moved = self._inventory(staged)
                        for item in moved:
                            item["path"] = (source / (self.runtime / item["path"]).relative_to(staged)).relative_to(self.runtime).as_posix()
                        expected = [item for item in plan["_entries"]
                                    if (self.runtime / item["path"]).is_relative_to(source)]
                        if sorted(moved, key=lambda item: item["path"]) != expected:
                            raise DeletionConflict("删除期间文件已变化，已恢复现场，请重新预览")
                    for kind, key in plan["_owned"]:
                        db.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, key))
                    for (kind, _), record in plan["_updates"].items():
                        _save(db, kind, {**record, "updated_at": now()})
                    receipt.update(state="purging", deleted_at=now())
                    _save(db, "deletion_receipt", receipt)
            except BaseException:
                self._restore(receipt)
                self.store.delete("deletion_receipt", task_id)
                raise
            return self._finish(receipt)

    @staticmethod
    def _result(receipt, pending):
        return {"task_id": receipt["id"], "deleted": True, "cleanup_pending": pending,
                "fingerprint": receipt["fingerprint"], "file_count": receipt["file_count"],
                "bytes": receipt["bytes"]}

    def _finish(self, receipt):
        try:
            self._purge(receipt)
        except (OSError, Conflict):
            return {**self._result(receipt, True),
                    "message": "任务记录已删除，本地文件清理未完成；使用原指纹重试"}
        receipt.update(state="completed", completed_at=now())
        for key in ("entries", "roots", "asset_ids"):
            receipt.pop(key, None)
        self.store.put("deletion_receipt", receipt)
        return self._result(receipt, False)
