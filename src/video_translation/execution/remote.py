"""Connect atomic project reservations to KIE's independently resumable journal."""

from __future__ import annotations

from video_translation.domain.limits import allows_new_call, allows_usage
import hashlib
import json
import math
import re
from contextlib import ExitStack
from pathlib import Path
from video_translation.application.tasks import Conflict
from video_translation.integrations.agents.errors import UnknownExternalResult
from video_translation.persistence.store import now
from video_translation.execution.credit_budget import check_kie_budget


def run_kie(service, config, task, node, kind, identity, action):
    from video_translation.integrations.providers.kie import KieClient

    key = (
        "kie-"
        + hashlib.sha256(
            json.dumps(
                {"task": task["id"], "node": node["id"], "kind": kind, "input": identity},
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
    )
    store = service.store
    estimate = None
    quote = None
    if kind == "video_edit":
        from video_translation.integrations.providers.video_pricing import video_estimate
        spec = task["model_snapshot"]["models"]["video"]
        estimate = video_estimate(spec, config.video_options["resolution"], identity["duration_ms"])
    elif kind == "image_edit":
        from video_translation.integrations.providers.image_pricing import image_quote
        quote = image_quote(identity.get("snapshot"))
        estimate = quote["estimated_credits"] if quote else None
    with store.connection() as db:
        current = json.loads(
            db.execute("SELECT data FROM records WHERE kind='task' AND id=?", (task["id"],)).fetchone()[0]
        )
        if current["status"] == "cancelled":
            raise Conflict("任务已取消")
        existing = db.execute("SELECT data FROM records WHERE kind='operation' AND id=?", (key,)).fetchone()
        if not existing:
            if kind == "video_edit" and current.get("review_draft_authorization"):
                raise Conflict("尽力审看流程未授权新增视频生成；仅恢复已有请求")
            if not config.api_key or not config.enabled_model_calls:
                raise Conflict("KIE调用未配置或未启用；未预留费用")
            if (kind == "image_edit" and current.get("review_draft_authorization", {}).get("image_provider")
                    == "codex_builtin_imagegen"):
                raise Conflict("用户指定内置imagegen，不允许转为KIE图片请求")
            if kind == "image_edit" and identity.get("snapshot") != current.get("image_snapshot"):
                raise Conflict("图片请求快照不属于当前任务；未预留费用")
            if kind == "video_edit" and current.get("clean_video_trial"):
                from video_translation.application.clean_video_trial import reserve_trial
                reserve_trial(current, task, identity, key, estimate)
            check_kie_budget(db, current, estimate)
            if not allows_new_call(current, 'max_paid_calls', current.get('paid_calls', 0)):
                raise Conflict("已达到外部调用次数上限；查询已有KIE任务不需要重新提交")
            if kind == "video_edit" and estimate is not None:
                committed = current.get("video_credits_committed", 0)
                if not allows_usage(current, 'max_video_credits', committed + estimate):
                    raise Conflict("视频模型累计预留额度不足；未知费用不释放，多语言与返工共用任务额度")
                current["video_credits_committed"] = committed + estimate
            current["paid_calls"] = current.get("paid_calls", 0) + 1
            current["updated_at"] = now()
            record = {
                "id": key,
                "task_id": task["id"],
                "node_id": node["id"],
                "provider": "kie",
                "kind": kind,
                "status": "running",
                "external_id": None,
                "estimated_cost": estimate,
                "actual_cost": None,
                "unit": "credits",
                "created_at": now(),
                "error": None,
            }
            if quote:
                record["quote"] = quote
            db.execute(
                "UPDATE records SET data=? WHERE kind='task' AND id=?",
                (json.dumps(current, ensure_ascii=False), task["id"]),
            )
            db.execute(
                "INSERT INTO records VALUES(?,?,?,?)",
                ("operation", key, json.dumps(record, ensure_ascii=False), record["created_at"]),
            )
    directory = service.settings.runtime / "tasks" / task["id"] / "nodes" / node["id"] / "remote"
    client = KieClient(config, directory=directory)

    def event(update):
        def apply(current):
            if update.get("task_id"):
                current["external_id"] = update["task_id"]
            credits = update.get("credits_consumed")
            if isinstance(credits, (int, float)) and not isinstance(credits, bool) and math.isfinite(credits):
                current["actual_cost"] = credits
            current["remote_phase"] = update.get("phase")
            if update.get("phase") == "fail":
                current["journal_state"] = "fail"

        store.update("operation", key, apply)

    try:
        result = action(client, key, event)
    except UnknownExternalResult as exc:
        if getattr(exc, "task_id", None):
            event({"task_id": exc.task_id, "phase": "pending"})
        store.update(
            "operation",
            key,
            lambda current: current.update(
                status="unknown", error="外部结果待核对；有taskId时可查询同一任务继续，无ID时禁止重投"
            ),
        )
        raise
    except Exception:
        store.update(
            "operation",
            key,
            lambda current: current.update(
                status="failed", error="KIE处理未完成；保留原请求与任务ID，不自动重复提交"
            ),
        )
        raise
    store.update(
        "operation", key, lambda current: current.update(status="succeeded", finished_at=now(), error=None,
                                                         journal_state="success", remote_phase="success",
                                                         recovery_mode="cached")
    )
    return result


def _journal_location(service, operation):
    runtime = Path(service.settings.runtime).resolve()
    task_id, node_id, key = (operation.get(name) for name in ("task_id", "node_id", "id"))
    if any(
        not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value)
        for value in (task_id, node_id, key)
    ):
        raise Conflict("KIE操作身份无效，不能定位恢复回执")
    directory = runtime / "tasks" / task_id / "nodes" / node_id / "remote"
    if not directory.resolve().is_relative_to(runtime):
        raise Conflict("KIE回执路径不属于当前运行目录")
    path = directory / (hashlib.sha256(key.encode()).hexdigest() + ".json")
    if path.is_symlink() or not path.resolve().is_relative_to(runtime):
        raise Conflict("KIE回执路径无法安全核验")
    return directory, path


def _inspect_journal(path, operation, task):
    from video_translation.integrations.providers.kie import KieClient, _url_shape
    from video_translation.integrations.agents.errors import ProviderError

    try:
        if not path.is_file() or not 0 < path.stat().st_size <= 8 * 1024 * 1024:
            raise ValueError
        raw = path.read_bytes()
        journal = json.loads(raw)
        if (
            not isinstance(journal, dict)
            or journal.get("version") != 1
            or not isinstance(journal.get("fingerprint"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", journal["fingerprint"])
        ):
            raise ValueError
        role = {"audio_understanding": "transcription", "synthesize": "tts", "video_edit": "video"}.get(
            operation.get("kind")
        )
        if operation.get("kind") == "image_edit":
            role = "image"
            expected_model = task.get("image_snapshot", {}).get("model")
        else:
            expected_model = task.get("model_snapshot", {}).get("models", {}).get(role, {}).get("model")
            trial = task.get("clean_video_trial") or {}
            if (role == "video" and trial.get("operation_key") == operation["id"]
                    and trial.get("node_id") == operation["node_id"]):
                expected_model = trial.get("model_snapshot", {}).get("models", {}).get("video", {}).get("model")
        if not role or not expected_model or journal.get("model") != expected_model:
            raise ValueError
        state, task_id = journal.get("state"), journal.get("task_id")
        credits = KieClient._credits(journal.get("credits_consumed"))
        if task_id is not None and (
            not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 512
        ):
            raise ValueError
        if operation.get("external_id") and operation["external_id"] != task_id:
            raise Conflict("KIE数据库taskId与本地回执不一致，必须先核对")
        blocked_reason = None
        if role == "transcription":
            # A synchronous chat has no task ID. Only fully validated cached content can resume.
            if task_id is not None:
                raise ValueError
            if state in {"submit-intent", "unknown", "response-invalid", "rejected"}:
                mode = "blocked"
                blocked_reason = "KIE同步请求无可验证成功缓存；结果不明时禁止重投"
            elif state == "success":
                transcript = journal.get("transcript")
                if (
                    not isinstance(transcript, dict)
                    or KieClient._transcript(json.dumps(transcript)) != transcript
                ):
                    raise ValueError
                mode = "cached"
            else:
                raise ValueError
        else:
            if not task_id and state in {"submit-intent", "unknown", "response-invalid", "rejected"}:
                mode = "blocked"
                blocked_reason = "KIE提交没有可核验taskId，不能重新付费提交"
            elif task_id and state in {"submitted", "waiting", "queuing", "generating", "success", "fail"}:
                mode = "poll"
                if state == "success":
                    urls = journal.get("result_urls")
                    if (
                        not isinstance(urls, list)
                        or len(urls) != 1
                        or not isinstance(urls[0], str)
                        or not isinstance(journal.get("result_object", {}), dict)
                    ):
                        raise ValueError
                    _url_shape(urls[0])
                    mode = "cached"
            else:
                raise ValueError
        return {
            "external_id": task_id,
            "actual_cost": credits,
            "resume_safe": mode != "blocked",
            "recovery_mode": mode,
            "recovery_reason": blocked_reason,
            "journal_state": state,
            "journal_sha256": hashlib.sha256(raw).hexdigest(),
        }
    except Conflict:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ProviderError):
        raise Conflict("KIE本地回执缺失、损坏或合同不匹配；必须先核对，不能重投") from None


def reconcile_kie_operations(service, task_id, *, node_id=None) -> list[dict]:
    """Reconcile unfinished KIE operations from authoritative local journals.

    Call BEFORE TaskService.retry enters a store transaction. This function performs
    no network calls and never deletes another worker's lock. Success returns the
    task's operation list with resume_safe/recovery_mode; an uncertain journal raises
    Conflict. A cached synchronous response has no fabricated external_id.
    """
    from video_translation.integrations.providers.kie import KieClient, KieConfig

    store = service.store
    task = store.require("task", task_id)
    operations = [op for op in store.list("operation") if op.get("task_id") == task_id]
    candidates = [
        op
        for op in operations
        if op.get("provider") == "kie" and op.get("status") in {"unknown", "running", "failed"}
        and (node_id is None or op.get("node_id") == node_id)
    ]
    if not candidates:
        return operations
    node_ids = {node["id"] for node in task.get("nodes", [])}
    observations = {}
    # Keep every acquired request lock until the database reconciliation commits.
    with ExitStack() as locks:
        for operation in candidates:
            if operation.get("node_id") not in node_ids:
                raise Conflict("KIE操作不属于当前任务节点，不能自动恢复")
            directory, path = _journal_location(service, operation)
            reader = KieClient(KieConfig(), directory=directory)
            try:
                locks.enter_context(reader._locked(path))
            except (UnknownExternalResult, OSError):
                raise Conflict("KIE请求锁仍存在或不可读取；不能判断进程状态，不抢锁、不重投") from None
            observations[operation["id"]] = _inspect_journal(path, operation, task)
        with store.connection() as db:
            for operation in candidates:
                row = db.execute(
                    "SELECT data FROM records WHERE kind='operation' AND id=?", (operation["id"],)
                ).fetchone()
                if row is None:
                    raise Conflict("KIE操作记录在恢复核验期间变化")
                current = json.loads(row[0])
                if any(
                    current.get(name) != operation.get(name)
                    for name in ("task_id", "node_id", "provider", "kind")
                ):
                    raise Conflict("KIE操作身份在恢复核验期间变化")
                observed = observations[operation["id"]]
                if current.get("external_id") and current["external_id"] != observed["external_id"]:
                    raise Conflict("KIE外部任务身份冲突，不能自动恢复")
                if observed["actual_cost"] is None:
                    observed = {key: value for key, value in observed.items() if key != "actual_cost"}
                current.update(observed, updated_at=now(), remote_phase="reconciled")
                # Preserve operation status: downloading/processing cached results may still fail.
                db.execute(
                    "UPDATE records SET data=? WHERE kind='operation' AND id=?",
                    (json.dumps(current, ensure_ascii=False), operation["id"]),
                )
    blocked = next((item for item in observations.values() if not item["resume_safe"]), None)
    if blocked:
        raise Conflict(blocked["recovery_reason"])
    return [op for op in store.list("operation") if op.get("task_id") == task_id]
