"""Export and verify a project-only public tree without Git or production writes."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = {"src", "contracts", "workflows", "prompts", "knowledge", "skills"}
CONFIG_FILES = (
    "agents/environment-map.yaml", "agents/harness.patch.yaml",
    "application/local.example.json", "application/runtime.toml",
    "integrations/fireplay/bridge-contract.yaml", "integrations/providers/environment-map.yaml",
    "models/catalog.json", "models/selection.json",
)
SCRIPTS = (
    "bootstrap.py", "setup.py", "install_codex.py", "install_harness.py",
    "install_separation.py", "check_kie.py", "configure_fireplay.py",
    "codex_bridge.py", "codex_events.py", "harness_bridge.py", "harness_guard.mjs",
    "import_kie_settings.py", "separate_audio.py", "cleanup.py", "export_public_source.py",
)
UI_FILES = (
    "index.html", "package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml",
    "tsconfig.json", "vite.config.ts",
)
PUBLIC_DOCS = {
    "README.md": "README.md",
    ".gitignore": "gitignore.template",
    "docs/architecture.md": "architecture.md",
    "docs/storage-and-performance.md": "storage-and-performance.md",
    "docs/source-release.md": "source-release.md",
    "docs/iteration-plan.md": "iteration-plan.md",
    "docs/temporal-removal-research.md": "temporal-removal-research.md",
    "docs/operations/clone-install.md": "clone-install.md",
}
EXCLUDED = {
    ".git", "runtime", "node_modules", "dist", "__pycache__", "tests",
    "test-results", "playwright-report", ".pytest_cache", ".ruff_cache",
}
TEXT_SUFFIXES = {
    ".py", ".json", ".yaml", ".yml", ".md", ".toml", ".ts", ".tsx",
    ".js", ".mjs", ".cjs", ".css", ".html", ".cmd", ".txt", ".license",
}
PRIVATE_NAMES = {
    "auth.json", "cookies.json", "cookies.txt", "credentials.json",
    "token.json", "token.txt", "storage-state.json", "storage_state.json", "local.json",
}
SECRET_PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    "api-key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{16,}"),
    "url-credentials": re.compile(r"https?://[^/\s:@]+:[^/\s@]+@"),
    "literal-credential": re.compile(
        r"""(?i)["'](?:api_key|access_token|refresh_token|password)["']\s*:\s*["'][A-Za-z0-9_./+=-]{24,}["']"""
    ),
}
MANIFEST = "SOURCE-MANIFEST.json"


def regular(path: Path) -> None:
    info = path.lstat()
    if (path.is_symlink() or getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
        raise ValueError(f"Linked source requires review: {path.name}")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Not a regular source file: {path.name}")


def check_directory(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or path.is_symlink()
            or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
        raise ValueError(f"Linked or invalid source directory: {path.name}")


def safe_relative(value: str) -> Path:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts or ":" in value or "\\" in value:
        raise ValueError("Invalid manifest relative path")
    return path


def scan(relative: Path, data: bytes) -> None:
    if len(data) > 10 * 1024**2:
        raise ValueError(f"Source file exceeds review limit: {relative.as_posix()}")
    if (relative.suffix.lower() not in TEXT_SUFFIXES
            and relative.name not in {".gitignore"}):
        raise ValueError(f"Unexpected source file type: {relative.as_posix()}")
    text = data.decode("utf-8-sig")
    for rule, pattern in SECRET_PATTERNS.items():
        if pattern.search(text):
            raise ValueError(f"Public-content check failed ({rule}): {relative.as_posix()}")


def tree(directory: Path):
    check_directory(directory)
    for base, directories, names in os.walk(directory, followlinks=False):
        parent = Path(base)
        kept = []
        for name in sorted(directories):
            if name in EXCLUDED or name.startswith(".") or name.endswith(".egg-info"):
                continue
            check_directory(parent / name)
            kept.append(name)
        directories[:] = kept
        for name in sorted(names):
            path = parent / name
            if (name.startswith(".") or name in PRIVATE_NAMES or name.endswith(".local.json")
                    or path.suffix.lower() in {".pyc", ".pyo", ".tsbuildinfo", ".log"}):
                continue
            regular(path)
            yield path


def inventory(root: Path) -> list[tuple[str, Path, dict]]:
    selected = {}

    def add(path, target=None):
        for parent in path.parents:
            check_directory(parent)
            if parent == root:
                break
        regular(path)
        relative = safe_relative(target or path.relative_to(root).as_posix())
        data = path.read_bytes()
        scan(relative, data)
        key = relative.as_posix()
        if key in selected:
            raise ValueError(f"Duplicate public source path: {key}")
        selected[key] = (path, {"path": key, "bytes": len(data),
                               "sha256": hashlib.sha256(data).hexdigest()})

    for name in ("pyproject.toml", "启动工作台.cmd"):
        add(root / name)
    for name in sorted(DIRECTORIES):
        for path in tree(root / name):
            add(path)
    for name in CONFIG_FILES:
        add(root / "config" / name)
    for name in SCRIPTS:
        add(root / "scripts" / name)
    for name in UI_FILES:
        add(root / "ui" / name)
    for path in tree(root / "ui" / "src"):
        add(path)
    curated = root / "docs" / "public"
    for target, source in PUBLIC_DOCS.items():
        add(curated / source if curated.exists() else root / target, target)
    return [(key, *selected[key]) for key in sorted(selected)]


def verify(destination: Path) -> dict:
    check_directory(destination)
    manifest_path = destination / MANIFEST
    regular(manifest_path)
    manifest = json.loads(manifest_path.read_text("utf-8"))
    if manifest.get("schema") != 1 or not isinstance(manifest.get("files"), list):
        raise ValueError("Unknown source manifest contract")
    expected = {MANIFEST}
    for row in manifest["files"]:
        relative = safe_relative(row["path"])
        if relative.as_posix() in expected:
            raise ValueError("Duplicate source manifest entry")
        expected.add(relative.as_posix())
        path = destination / relative
        for parent in path.parents:
            if parent == destination:
                break
            check_directory(parent)
        regular(path)
        data = path.read_bytes()
        scan(relative, data)
        if len(data) != row["bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise ValueError(f"Public source differs from manifest: {relative.as_posix()}")
    actual = set()
    for base, directories, names in os.walk(destination, followlinks=False):
        parent = Path(base)
        if parent == destination:
            directories[:] = [name for name in directories if name != ".git"]
        for name in directories:
            check_directory(parent / name)
        for name in names:
            path = parent / name
            regular(path)
            actual.add(path.relative_to(destination).as_posix())
    if actual != expected:
        raise ValueError("Public tree has missing or additional files")
    return {"passed": True, "files": len(manifest["files"]),
            "bytes": sum(row["bytes"] for row in manifest["files"]),
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "published": False}


def export(root: Path, destination: Path) -> dict:
    root = root.resolve()
    destination = destination.absolute()
    resolved = destination.resolve()
    releases = root / "runtime" / "releases"
    if (root.is_relative_to(resolved)
            or resolved.is_relative_to(root) and not resolved.is_relative_to(releases)):
        raise ValueError("Export requires an independent directory or an excluded runtime/releases location")
    if destination.exists() or destination.is_symlink():
        raise ValueError("Export destination must not exist; no existing directory is overwritten")
    rows = inventory(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    for parent in destination.parents:
        check_directory(parent)
    destination.mkdir()
    for relative, source, row in rows:
        data = source.read_bytes()
        if len(data) != row["bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise ValueError(f"Source changed during export: {relative}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)
    if [row for _, _, row in inventory(root)] != [row for _, _, row in rows]:
        raise ValueError("Source changed during export; do not publish the incomplete tree")
    manifest = {
        "schema": 1, "scope": "project-only-public-source",
        "files": [row for _, _, row in rows],
        "excluded": ["runtime", "production-media", "test-sources", "test-results",
                     "local-config", "credentials", "installed-dependencies",
                     "historical-verification", "prior-git-history"],
        "quality_acceptance": "Source verification does not establish media quality or user approval.",
    }
    with (destination / MANIFEST).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return verify(destination)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("export", "verify"))
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    result = export(ROOT, args.destination) if args.action == "export" else verify(args.destination)
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
