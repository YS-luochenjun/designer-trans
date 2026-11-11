"""Trusted entry wrapper: wait for the parent fence, then limit and load a script."""
import os
from pathlib import Path
import runpy
import sys


def main():
    script, root, memory = sys.argv[1:]
    if sys.stdin.buffer.readline() != b"go\n":
        raise SystemExit(2)
    if os.name != "nt":
        import resource
        maximum = int(memory)
        resource.setrlimit(resource.RLIMIT_AS, (maximum, maximum))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    sys.argv = [script, root]
    sys.path.insert(0, str(Path(script).parent))
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
