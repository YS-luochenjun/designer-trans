"""Versioned executor facts supplied independently of frozen model knowledge."""

REVISION = "local-clean-v8-image-pipeline"


def execution_contract():
    return {
        "revision": REVISION,
        "ownership": "Work agent proposes inspect/repair/review actions; executor renders and retains evidence.",
        "pipeline_diagnosis": (
            "inspect_pipeline selects an exact current or archived candidate. image_edit retains "
            "input/generated/conformed PNGs; static images have no video timestamps or frame numbers. "
            "Rejected pixels remain rejected. Missing historical pipeline registration is a diagnostic "
            "limit, not proof of file loss or inability to make new repairs; select a different viable method."
        ),
        "host_image_reference": (
            "When available_tools advertises image_reference, request_reference selects source_id, "
            "one supplied original time, a removal-only prompt and evidence. A source/base-bound "
            "Project-owned Codex worker consumes the durable request with native tools; "
            "no developer chat is required. Unavailable tools return a retained failure; "
            "uncertain calls are not regenerated. Its returned still is separately labelled synthetic. "
            "reference_plate uses an admitted reference_id with work-selected short geometry/time. "
            "A still is not temporal restoration or video acceptance."
        ),
        "review_draft": (
            "An incomplete best-effort clean preview retains unresolved intervals and "
            "quality_passed=false in the clean node. It is diagnostic media, not a qualified "
            "input for lettering. Complete clean-plate review is required before lettering; "
            "do not add coverage, waive readable residue, or authorize new paid video calls."
        ),
        "coverage": "Boundary-reviewed local edits use actual CFR display bounds. Never repair a ledger-only gap twice.",
        "defer_scope": (
            "defer/blocked with source_id and start_ms/end_ms blocks only that observed interval. "
            "Queue exposes actionable_intervals_ms and deferred_intervals_ms; neither is accepted coverage. "
            "One blurred transition cannot establish that the whole remaining source is infeasible. "
            "Inspect separate stable windows and boundaries; omit bounds only with whole-source evidence. "
            "Legacy unbounded deferrals remain blocked until an audited reassessment."
        ),
        "archive_diagnosis": (
            "inspect_archive returns source/base/archive images AND archive_diagnostics: retained repair "
            "geometry, foreground polygons, donor and eligible frame range. sampled_pixel_changes compares "
            "supplied decoded pairs against the retained historical input after task/path/SHA verification, "
            "even when the current base has advanced. Four image roles are separately labelled, at most24 images. "
            "Missing or invalid historical input disables measurement; current base cannot substitute for it. "
            "Pixel changes do not prove quality. "
            "Reconcile image roles/frame indices with measured evidence. Diagnose overprotection and incomplete "
            "coverage from parameters plus images before making an evidenced revision."
        ),
        "acceptance_conflict": (
            "While inspecting an accepted archive, blocked/defer with candidate_id, matching review_sha256, "
            "source_id, explicit quality.hard_failures and actual edited-frame evidence registers a dispute. "
            "The original verdict and media remain; its coverage and method advisory are excluded pending "
            "fresh current-plate scope review. Pixel changes and old scores cannot resolve that review."
        ),
        "foreground": {
            "implemented": True,
            "parameter": "protected_foreground",
            "methods": ["tracked_donor", "local_surface"],
            "coordinates": "Normalized source-frame x/y, absolute integer time_ms.",
            "limits": "At most32 polygon keyframes;3-32 matching ordered vertices. Coordinates interpolate.",
            "effect": "Pixels inside the observed polygon are preserved from the current base.",
            "requires": "Observe actual occluder geometry across the short interval; include motion extremes.",
            "not_implemented": ["automatic segmentation", "optical flow", "perspective registration"],
        },
        "surface_axes": {
            "horizontal": "Requires actual clean left and right margins.",
            "vertical": "Requires actual clean top and bottom margins.",
            "gate": "Unchanged margin difference threshold; masked pixels do not waive the margin check.",
        },
        "surface_models": {
            "linear": "Default; horizontal/vertical margin difference gates remain unchanged.",
            "plane": (
                "surface_repair.model=plane fits RGB lighting planes from a three-pixel ring on ALL four "
                "sides of each observed repair rectangle in the current frame. Requires NumPy. "
                "Every ring pixel must fit with max error<=12 and RMS<=4; fail on texture, hair, "
                "lettering, a hard lighting edge or missing margins. Fixed gates cannot be relaxed. "
                "Never reads the erased interior as background; preserves explicit foreground polygons. "
                "Only smooth low-texture lighting gradients; not inpainting, segmentation or texture recovery. "
                "Inspect complete glyph outlines and sampling bands across the short window first. "
                "A successful fit is not proof of clean bands, hidden structure or candidate quality."
            ),
        },
        "routing": [
            "Consult video-translation.methods before deferring for a supposedly missing local capability.",
            "A moving foreground is not alone proof that explicit polygon protection is unavailable.",
            "Use observed same-shot donor and polygon protection only when their geometry and background match.",
            "Inspect source/base/candidate inner and outer boundary frames; reject readable residue or subject damage.",
            "Do not invent automatic masks, weaken thresholds, replay unknown calls, or claim whole-video acceptance.",
        ],
    }
