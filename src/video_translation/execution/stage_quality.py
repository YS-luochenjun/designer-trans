"""Hard prerequisites independent of cosmetic tolerance or production mode."""
from video_translation.integrations.agents.errors import CapabilityUnavailable


def require_clean_plate(output, task=None):
    review = output.get("clean_review") or {}
    if (output.get("completion_state") == "best_available"
            or output.get("quality_passed") is not True or output.get("unresolved")
            or not output.get("sha256") or review.get("sha256") != output["sha256"]
            or review.get("scope") != "clean_plate" or not review.get("evidence")):
        raise CapabilityUnavailable("无字底片尚未清理完成，禁止叠加译文；先处理残字并复核底片")


def require_production_base(output, task=None):
    """Output intent permits diagnostic exports, never unfinished lettering inputs."""
    require_clean_plate(output, task)


def require_visual_review(output, task=None):
    if output.get("render_error"):
        raise CapabilityUnavailable("译文字层渲染失败，不能作为可合成画面；先在字层节点修复")
    if (output.get("visual_review") == "no_translated_text_required"
            and not output.get("layers") and not output.get("plan")
            and output.get("clean_review", {}).get("source_ids") == []):
        require_clean_plate(output, task)
        return
    from .review_draft import is_review_draft
    if is_review_draft(output, task) and output.get("visual_review") in {
            "draft_checked_with_issues", "agent_checked"}:
        return
    if output.get("visual_review") not in {"agent_checked", "human_approved"}:
        raise CapabilityUnavailable("译文大小、颜色、位置和运动尚未通过对照检查，不能直接合成成品")


def require_node_output(task, node, output):
    """Validate a node's business result before marking dependencies complete."""
    if task.get("visual_route") != "agent_adaptive":
        return
    stage = node["definition_id"]
    observed = next((n for n in task.get("nodes", [])
                     if n["definition_id"] == "observe" and n["asset_id"] == node["asset_id"]), None)
    required = ({t["id"] for t in observed["output"].get("texts", [])
                 if t["treatment"] == "translate"} if observed else None)
    if stage == "clean":
        require_production_base(output, task)
        if required is not None and set(output["clean_review"].get("source_ids", [])) != required:
            raise CapabilityUnavailable("清底复核未逐项覆盖原片待译文字，不能标记完成")
    elif stage == "typography":
        if output.get("render_error"):
            raise CapabilityUnavailable("译文字层没有生成，保留渲染错误，不能标记完成")
        expected = {p["source_id"] for p in output.get("plan", [])}
        if required is not None and expected != required:
            raise CapabilityUnavailable("译文字层遗漏原片待译实例，不能标记完成")
        planned = next((n for n in task.get("nodes", [])
                        if n["definition_id"] == "plan" and n["asset_id"] == node["asset_id"]
                        and n["locale"] == node["locale"]), None)
        if planned and expected != {p["source_id"] for p in planned["output"].get("lettering", [])}:
            raise CapabilityUnavailable("译文字层遗漏语言适配计划中的实例，不能标记完成")
        rendered = [layer["source_id"] for layer in output.get("layers", [])]
        if expected != set(rendered) or len(rendered) != len(set(rendered)):
            raise CapabilityUnavailable("译文字层产物不完整，不能标记完成")
    elif stage == "visual":
        require_visual_review(output, task)


def require_motion_evidence(source):
    status = source.get("motion_status", "unknown")
    if status == "unknown" or len(set(source.get("motion_evidence_times_ms", []))) < 2:
        raise CapabilityUnavailable("文字运动未核实，不能把未知轨迹默认成静态文字")
    if status == "dynamic" and len(source.get("motion", [])) < 2:
        raise CapabilityUnavailable("动态文字缺少实际轨迹关键帧，不能按单帧位置叠字")
