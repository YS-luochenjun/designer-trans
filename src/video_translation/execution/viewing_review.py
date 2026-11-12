"""Evidence checks for viewing-impact verdicts; never changes scores or pixels."""

HARD_FAILURES = {"readable_source_text", "subject_damage", "new_object", "severe_flicker"}


def assess_review(quality, observed_frames, pass_score=80):
    if not quality:
        return "clarify"
    findings = quality.get("findings", [])
    failures = set(quality.get("hard_failures", []))
    major = set()
    for finding in findings:
        frames = set(finding["frame_indices"])
        if (not frames or not frames <= set(observed_frames)
                or not finding["observation"].strip() or not finding["viewing_impact"].strip()
                or finding["severity"] == "uncertain"):
            return "clarify"
        category = finding["category"]
        if finding["severity"] == "major":
            major.add(category)
        else:
            if category in HARD_FAILURES:
                return "clarify"
    if failures - major or (major & HARD_FAILURES) - failures:
        return "clarify"
    if failures:
        return "reject"
    score = sum(quality[name] for name in
                ("text_removal", "subject_preservation", "temporal_consistency"))
    if score >= pass_score:
        return "accept"
    # A low score requires more than a list of explicitly minor differences.
    if not major:
        return "clarify"
    return "reject"


def enabled(policy):
    return policy.get("viewing_standard", {}).get("evidence_gate", False)


def clarification_times(findings, clock, candidate):
    from .frame_clock import boundary_indices, request_time

    indices = set(boundary_indices(candidate["start_ms"], candidate["end_ms"],
                                   clock["rate"], clock["frames"]))
    affected = sorted({i for f in findings for i in f["frame_indices"]})
    for frame in affected:
        for index in (frame, frame - 1, frame + 1):
            if 0 <= index < clock["frames"] and len(indices) < 8:
                indices.add(index)
    return sorted(request_time(i, clock["rate"]) for i in indices)


def guard_candidate_verdict(controller, state, action, frames, policy, policy_ref):
    """Return True after scheduling one evidence clarification, preserving media."""
    if not enabled(policy) or action["action"] not in {"accept", "reject_candidate"}:
        return False
    candidate = state.get("candidate")
    if not candidate or candidate["id"] != action.get("candidate_id"):
        return False
    observed = {f["frame_index"] for f in frames
                if "当前局部修复候选" in f.get("label", "")}
    verdict = assess_review(action.get("quality"), observed, policy["pass_score"])
    if verdict != "clarify" and (
            action["action"] == "accept" and verdict == "accept"
            or action["action"] == "reject_candidate" and verdict == "reject"):
        return False
    from .clean_progress import fingerprint
    key = fingerprint({"candidate": candidate["sha256"], "base": state["base"]["sha256"],
                       "policy": policy_ref["sha256"]})
    question = ("评分或硬伤标签与已观察证据不一致。填写quality.findings：具体帧号、"
                "minor/major/uncertain、实际变化及观看影响；一两帧非文字轻微变化不自动是硬伤。"
                "不修改媒体或重投模型，原分数不自动提高，静帧不冒充播放。")
    state["history"][-1]["tool_error"] = question
    attempts = state.setdefault("quality_clarifications", {})
    if key in attempts:
        candidate["review_status"] = "needs_human_review"
        candidate["review_question"] = question
        state["turn"] += 1
        controller.store.put("production_session", state)
        from video_translation.integrations.agents.errors import CapabilityUnavailable
        raise CapabilityUnavailable("候选评判证据仍矛盾，保留候选供人工判断；未登记结构损伤禁用或新增付费请求")
    attempts[key] = {"turn": state["turn"], "candidate_id": candidate["id"],
                     "policy": policy_ref, "question": question}
    _, clock = controller.media._info(controller.source)
    state["times_ms"] = clarification_times(
        (action.get("quality") or {}).get("findings", []), clock, candidate)
    state["turn"] += 1
    controller.store.put("production_session", state)
    return True


def guard_review_only(controller, state, action):
    candidate = state.get("candidate") or {}
    if not candidate.get("quality_reassessment") or action["action"] in {
            "inspect", "inspect_metadata", "inspect_pipeline", "consult", "accept", "reject_candidate", "blocked", "defer"}:
        return
    state["history"][-1]["tool_error"] = "本次仅复核已保存候选，不授权生成、换候选或修改媒体"
    state["turn"] += 1
    controller.store.put("production_session", state)
    from video_translation.integrations.agents.errors import CapabilityUnavailable
    raise CapabilityUnavailable("复核请求超出只读审阅范围，候选和旧回执已保留；没有新增生成")
