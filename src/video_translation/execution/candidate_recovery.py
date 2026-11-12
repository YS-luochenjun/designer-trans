"""Read-only archive discovery and conservative local rebasing eligibility."""
from .frame_clock import boundary_indices, frame_window, request_time, sample_index


def retained_archive_evidence(controller, state, candidate, turn, clock):
    """Supply the real archived input, independently of subsequent accepted edits."""
    expected = candidate.get("input_sha256")
    references = [state["base"], *state.get("candidate_archive", []), *state.get("accepted", [])]
    references.append({"path": str(controller.source), "sha256": expected})
    baseline = None
    for reference in references:
        if not expected or reference.get("sha256") != expected or not reference.get("path"):
            continue
        try:
            baseline = controller.verified(reference)
            break
        except (ValueError, OSError):
            continue
    # Four roles share at most24 images; retain both inner/outer boundaries.
    required = [request_time(i, clock["rate"]) for i in boundary_indices(
        candidate["start_ms"], candidate["end_ms"], clock["rate"], clock["frames"])]
    limit = 6 if baseline is not None else 8
    requested = state.get("archive_requested_times")
    if requested:
        # Explicit follow-up evidence takes priority over optional repeated boundaries.
        unique = {sample_index(t, clock["rate"], clock["frames"]): t for t in requested}
        if len(unique) > limit:
            raise ValueError("历史三方/四方检查超过图片上限；请缩小时间点，未替换所请求的帧")
        chosen = list(unique.values())
        times = sorted(chosen + [t for t in required if sample_index(
            t, clock["rate"], clock["frames"]) not in unique][:limit - len(chosen)])
    else:
        times = sorted(set(required + [
            t for t in state["times_ms"] if t not in required][:limit - len(required)]))
    frames = controller.frames(controller.source, times, "原片", f"archive-source-{turn}")
    frames += controller.frames(controller.verified(state["base"]), times,
                                "当前底片", f"archive-base-{turn}")
    if baseline is not None:
        frames += controller.frames(baseline, times, "历史输入底片（SHA核验）", f"archive-input-{turn}")
    frames += controller.frames(controller.verified(candidate), times,
                                "历史候选（非当前候选，禁止直接接受）", f"archive-old-{turn}")
    changes = sampled_candidate_changes(
        candidate, expected if baseline is not None else None, frames,
        baseline_label="历史输入底片", candidate_label="历史候选")
    changes["historical_input_verified"] = baseline is not None
    changes["supplied_times_ms"] = times
    if baseline is None:
        changes["reason"] = "Retained input missing or failed task/path/SHA verification; no attribution"
    return frames, changes


def candidate_diagnostics(candidate, clock):
    """Retained executor parameters, not a claim about decoded changed pixels."""
    fields = ("id", "source_id", "method", "start_ms", "end_ms", "region", "sha256",
              "input_sha256", "donor_time_ms", "donor_region", "surface_repair",
              "tracked_donor", "protected_foreground", "review_status", "rejection")
    result = {k: candidate[k] for k in fields if k in candidate}
    first, stop = frame_window(candidate["start_ms"], candidate["end_ms"], clock["rate"])
    stop = min(stop, clock["frames"])
    result["execution_window"] = {
        "rate": clock["rate"], "first_frame": first, "end_frame_exclusive": stop,
        "last_frame": stop - 1, "changed_pixels_measured": False,
        "meaning": "Frames eligible for the edit; foreground mask may protect some or all pixels.",
    }
    if candidate.get("donor_time_ms") is not None:
        result["donor_frame"] = sample_index(candidate["donor_time_ms"], clock["rate"], clock["frames"])
    return result


def sampled_candidate_changes(candidate, base_sha256, frames, *, baseline_label, candidate_label):
    """Compare supplied decoded frame pairs only when the input base still matches."""
    if candidate.get("input_sha256") != base_sha256:
        return {"measured": False, "reason": "Current base differs from the candidate input; no attribution"}
    from PIL import Image, ImageChops

    baseline = {f["frame_index"]: f for f in frames if baseline_label in f.get("label", "")}
    edited = {f["frame_index"]: f for f in frames if candidate_label in f.get("label", "")}
    changes = []
    for index in sorted(baseline.keys() & edited.keys()):
        with Image.open(baseline[index]["path"]) as before, Image.open(edited[index]["path"]) as after:
            before, after = before.convert("RGB"), after.convert("RGB")
            if before.size != after.size:
                raise ValueError("Candidate diagnostic frames have different geometry")
            bbox = ImageChops.difference(before, after).getbbox()
        changes.append({"frame_index": index, "changed": bool(bbox), "changed_bbox_px": bbox})
    return {
        "measured": bool(changes), "baseline_sha256": base_sha256,
        "candidate_sha256": candidate["sha256"], "sampled_frames": changes,
        "limitation": "Only supplied decoded frame pairs. A changed rectangle does not prove text removal or quality.",
    }


def overlaps(a, b):
    if max(a["start_ms"], b["start_ms"]) >= min(a["end_ms"], b["end_ms"]):
        return False
    x, y = a["region"], b["region"]
    return (max(x["x"], y["x"]) < min(x["x"] + x["width"], y["x"] + y["width"])
            and max(x["y"], y["y"]) < min(x["y"] + x["height"], y["y"] + y["height"]))


def rebase_obstacle(candidate, state):
    if candidate.get("review_status") == "needs_human_review":
        return "审阅额度用尽的候选已保留待人工判断，不能通过重合成重置"
    if candidate.get("review_status") == "rejected":
        return "已拒绝候选只能诊断，不能恢复"
    if candidate["id"] in {c["id"] for c in state["accepted"]}:
        return "该候选已经接受，不重复恢复"
    if candidate.get("input_sha256") == state["base"]["sha256"]:
        return None
    chain = state["accepted"]
    index = next((i for i, c in enumerate(chain)
                  if c["sha256"] == candidate.get("input_sha256")), None)
    if index is None and chain and chain[0].get("input_sha256") == candidate.get("input_sha256"):
        index = -1
    if index is None:
        return "旧基底不在当前接受链，须人工核对来源"
    if not chain or chain[-1]["sha256"] != state["base"]["sha256"]:
        return "接受链与当前底片不一致，不能自动重合成"
    if any(overlaps(candidate, c) for c in chain[index + 1:]):
        return "后续已接受结果与旧补片时空重叠，禁止覆盖"
    return None


def archive_summary(state):
    return [{**{k: c.get(k) for k in (
        "id", "source_id", "method", "start_ms", "end_ms", "review_status", "rejection")},
        "has_pipeline": bool(c.get("pipeline")),
        "rebase_obstacle": rebase_obstacle(c, state),
    } for c in state.get("candidate_archive", [])]
