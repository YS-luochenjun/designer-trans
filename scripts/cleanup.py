"""Preview or remove only reviewed regenerable project caches. Stop workers first."""

from __future__ import annotations
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import stat

ROOT = Path(__file__).resolve().parents[1]
TARGETS = (
    "runtime/tmp",
    "runtime/cache/install-archive",
    "runtime/cache/pytest",
    "runtime/browser-acceptance",
    "runtime/browser-workspace",
    "runtime/browser-temp",
    "runtime/browser-source.mp4",
    ".pytest_cache",
    ".ruff_cache",
)
SOURCE_DIRS = ("src", "tests", "scripts")


def inspect_tree(root: Path, relative: str):
    target = root / relative
    if not target.exists():
        return None
    # Refuse links/junctions at any path component, including the cleanup root.
    for part in [root, *target.relative_to(root).parents]:
        path = part if part.is_absolute() else root / part
        if path.exists() and (path.is_symlink() or getattr(path, "is_junction", lambda: False)()):
            raise ValueError("Refusing linked cleanup path")
    paths = [target]
    if target.is_dir():
        for base, dirs, names in os.walk(target, followlinks=False):
            paths.extend(Path(base) / name for name in [*dirs, *names])
    size = 0
    for path in paths:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Refusing linked/reparse cleanup entry")
        if path.is_file():
            size += info.st_size
    return {"path": relative, "bytes": size, "entries": len(paths)}


def remove_tree(path):
    def readonly_file(function, target, error):
        info = os.lstat(target)
        if (
            isinstance(error, PermissionError)
            and stat.S_ISREG(info.st_mode)
            and not info.st_mode & stat.S_IWRITE
        ):
            os.chmod(target, info.st_mode | stat.S_IWRITE)
            function(target)
        else:
            raise error

    shutil.rmtree(path, onexc=readonly_file)


def cleanup(root=ROOT, apply=False):
    from video_translation.persistence.store import WorkerLock

    root = root.resolve()
    import re

    targets = list(TARGETS)
    root_temporary = re.compile(
        r"pip-(?:build-env|build-tracker|ephem-wheel-cache|install|metadata|unpack)-[A-Za-z0-9_-]+"
    )
    targets += [entry.name for entry in root.iterdir() if root_temporary.fullmatch(entry.name)]
    targets.append("ui/tsconfig.tsbuildinfo")
    for directory in SOURCE_DIRS:
        base = root / directory
        if base.exists():
            for parent, dirs, _ in os.walk(base, followlinks=False):
                if "__pycache__" in dirs:
                    targets.append((Path(parent) / "__pycache__").relative_to(root).as_posix())
                    dirs.remove("__pycache__")
    reviewed = [item for target in targets if (item := inspect_tree(root, target))]
    report = {
        "apply": apply,
        "targets": reviewed,
        "bytes": sum(item["bytes"] for item in reviewed),
        "business_data_touched": False,
    }
    if not apply:
        return report
    with ExitStack() as stack:
        for directory in ("runtime", "runtime/browser-acceptance"):
            lock_path = root / directory / "worker.lock"
            if lock_path.exists():
                guard = WorkerLock(lock_path)
                guard.acquire()
                stack.callback(guard.release)
        for item in reviewed:
            target = root / item["path"]
            inspect_tree(root, item["path"])
            # Don't hold a Windows handle inside a tree that is being deleted.
            if item["path"] == "runtime/browser-acceptance":
                for child in target.iterdir():
                    if child.name != "worker.lock":
                        remove_tree(child) if child.is_dir() else child.unlink()
            else:
                remove_tree(target) if target.is_dir() else target.unlink()
    browser = root / "runtime/browser-acceptance"
    if browser.exists():
        (browser / "worker.lock").unlink(missing_ok=True)
        browser.rmdir()
    output = root / "runtime/logs/cleanup-receipt.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(cleanup(apply=args.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
