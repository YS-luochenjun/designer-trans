from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path

import yaml


class ResourceCatalog:
    def __init__(self, root: Path):
        self.root = root

    def list(self, kind: str) -> list[dict]:
        if kind not in {"prompts", "knowledge", "skills"}:
            raise ValueError("未知资源类别")
        base = self.root / kind
        result = []
        for manifest_path in sorted(base.rglob("manifest.yaml")):
            manifest = yaml.safe_load(manifest_path.read_text("utf-8"))
            text_path = (manifest_path.parent / manifest["content"]).resolve()
            if not text_path.is_relative_to(base.resolve()):
                raise ValueError("资源正文必须位于所属目录")
            text = text_path.read_text("utf-8")
            result.append(
                {
                    "id": manifest["id"],
                    "version": manifest["version"],
                    "kind": kind,
                    "name": manifest.get("name", manifest["id"]),
                    "status": manifest.get("status", "active"),
                    "description": manifest.get("description", ""),
                    "nodes": manifest.get("nodes", []),
                    "path": str(manifest_path.relative_to(self.root)),
                    "content": text,
                    "sha256": hashlib.sha256(text.encode()).hexdigest(),
                }
            )
        return result

    def snapshot(self):
        return {kind: self.list(kind) for kind in ("prompts", "knowledge", "skills")}

    @staticmethod
    def _scoped(task: dict, node: dict) -> list[dict]:
        return [item for kind in ("prompts", "knowledge", "skills")
                for item in task["resource_snapshot"].get(kind, [])
                if item["status"] == "active" and (not item["nodes"] or node["definition_id"] in item["nodes"])]

    def consult(self, task: dict, node: dict, ids: list[str]) -> list[dict]:
        """Return frozen stage-authorized resources without rereading mutable files."""
        if (not isinstance(ids, list) or any(not isinstance(value, str) or not value.strip() for value in ids)
                or len(ids) != len(set(ids))):
            raise ValueError("查阅资源必须提供不重复的资源ID列表")
        by_id = {}
        for entry in self._scoped(task, node):
            if entry["id"] in by_id:
                raise ValueError("任务资源快照含歧义ID，不能查阅")
            by_id[entry["id"]] = entry
        if any(value not in by_id for value in ids):
            raise ValueError("资源ID不存在、未启用或不适用于当前阶段")
        selected = [by_id[value] for value in ids]
        for entry in selected:
            if hashlib.sha256(entry["content"].encode()).hexdigest() != entry["sha256"]:
                raise ValueError("任务资源快照正文与校验和不一致")
        return deepcopy(selected)

    @staticmethod
    def _proposals(inputs: dict) -> list[str]:
        # Fixed descriptive identifiers; never tool names or executable commands.
        known = {name: "propose:" + name for name in (
            "inspect", "track", "flat", "donor", "tracked_donor", "image_edit", "wan", "local_surface", "accept", "finish", "consult", "blocked",
            "repair", "revise_scope", "confirm_scope", "restore_candidate", "inspect_pipeline", "reject_candidate",
            "inspect_archive", "rebase_candidate", "defer", "image_reference", "reference_plate",
            "clarify", "font", "artwork")}
        available = inputs.get("available_tools", {})
        known["image_reference"] = "propose:request_reference"
        if not isinstance(available, dict):
            return []
        return [known[name] for name in known if name in available]

    def packet(self, task: dict, node: dict, inputs: dict, schema: dict) -> dict:
        from video_translation.application.collaboration import instructions_for
        from .production_context import production_context
        scoped = self._scoped(task, node)
        entries = scoped
        if inputs.get("resource_mode") == "on_demand":
            requested = self.consult(task, node, inputs.get("resource_ids", []))
            required = self.consult(task, node, inputs.get("required_resource_ids", []))
            selected = requested + required
            selected_ids = {entry["id"] for entry in selected}
            selected_ids.update(entry["id"] for entry in scoped
                                if entry["id"] == "video-translation.visual-fidelity")
            entries = [entry for entry in scoped if entry["kind"] == "prompts" or entry["id"] in selected_ids]
        prompt = "\n\n".join(f"[{e['kind']}:{e['id']}@{e['version']}]\n{e['content']}" for e in entries)
        autonomous_work = (task.get("agent_engine") == "codex" and inputs.get("agent_role") == "work")
        if autonomous_work:
            prompt += (
                "\n\n[运行端work工具权限]\n"
                "本轮work使用Codex原生工具运行环境。允许自主打开文件、补充观察、"
                "运行本地脚本、研究方法及使用实际可用工具；旧资源中的"
                "“只输出JSON/执行器实际加工”指最终业务回执，不禁止中间工具调用。"
                "制作结果须保存并经项目登记与审阅，不能直接改库或虚构接受。"
                "图片请求由项目自身Codex执行环节完成，不等待开发者聊天。"
                "外部收费仍通过项目统一预算，暂停取消与未知请求去重继续有效。"
            )
        index_fields = ("id", "name", "kind", "version", "description", "sha256")
        return {
            "schema_version": "1.0.0",
            "task_id": task["id"],
            "node_run_id": f"{node['id']}:{node['attempt']}",
            "stage": node["definition_id"],
            "locale": node["locale"],
            "notes": task["notes"],
            "production_context": production_context(task),
            "confirmed_node_instructions": instructions_for(task, node),
            "brief_revision": task.get("brief_revision", 0),
            "source_language": task["source_language"],
            "instruction": prompt,
            "inputs": inputs,
            "output_schema": schema,
            "resource_versions": [{k: entry[k] for k in ("id", "version", "sha256")} for entry in entries],
            "resource_index": [{key: deepcopy(entry[key]) for key in index_fields} for entry in scoped],
            "allowed_capabilities": self._proposals(inputs),
            "constraints": {"no_external_writes": not autonomous_work,
                            "structured_result_only": not autonomous_work,
                            "capabilities_are_proposals_only": not autonomous_work,
                            **({"final_business_json_required": True,
                                "project_cost_ledger_required": True} if autonomous_work else {})},
        }
