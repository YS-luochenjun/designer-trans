"""Small subprocess bridge; runs only in the installed FaceFusion environment."""
import json
from pathlib import Path
import sys


def main():
    root, recipe = Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text("utf-8"))
    sys.path.insert(0, str(root))
    output = Path(recipe["output"])
    config = output / "facefusion.ini"
    config.write_text("", encoding="utf-8")
    profile = recipe["profile"]
    sys.argv = ["facefusion.py", "headless-run", "--config-path", str(config),
                "--target-path", recipe["video"], "--face-selector-order", profile["face_selector_order"]]
    from facefusion.program import create_program
    from facefusion.args import apply_args
    from facefusion import state_manager, face_detector, face_landmarker, face_recognizer, face_classifier
    from facefusion.face_creator import get_many_faces
    from facefusion.face_selector import sort_and_filter_faces
    from facefusion.vision import read_static_video_frame, write_image
    apply_args(vars(create_program().parse_args()), state_manager.init_item)
    # Inspection reuses local models; it never invokes pre_check's download paths.
    for module in (face_detector, face_landmarker, face_recognizer, face_classifier):
        if hasattr(module, "collect_model_downloads"):
            _, sources = module.collect_model_downloads()
        else:
            sources = module.get_model_options().get("sources", {})
        if not sources or any(not Path(item["path"]).is_file() for item in sources.values()):
            raise RuntimeError("Required FaceFusion detection model is absent")
    frame = read_static_video_frame(recipe["video"], profile["reference_frame_number"])
    if frame is None:
        raise ValueError("Reference frame is unavailable")
    if not write_image(str(output / "frame.png"), frame):
        raise ValueError("Cannot save reference frame")
    faces = sort_and_filter_faces([], get_many_faces([frame]))
    members = []
    for index, face in enumerate(faces):
        x1, y1, x2, y2 = map(int, face.bounding_box)
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(frame.shape[1], x2), min(frame.shape[0], y2)
        if x2 <= x1 or y2 <= y1:
            raise ValueError("Invalid face bounds")
        if not write_image(str(output / f"face-{index}.png"), frame[y1:y2, x1:x2]):
            raise ValueError("Cannot save face crop")
        members.append({"position": index, "bounds": [x1, y1, x2, y2]})
    (output / "faces.json").write_text(json.dumps({
        "faces": members, "frame_number": profile["reference_frame_number"],
        "order": profile["face_selector_order"],
    }), encoding="utf-8")


if __name__ == "__main__":
    main()
