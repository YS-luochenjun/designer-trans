"""Pinned trusted tool processes with deadlines and process-tree resource fencing.

This is an execution boundary, not a security sandbox for hostile code. Engines
are never registered automatically and receive only copied evaluation frames.
"""
import ctypes
from ctypes import wintypes
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

from PIL import Image

from .media import MediaError
from .workspace import file_sha, publish_json


@contextmanager
def engine_workspace(scratch_root):
    workspace = tempfile.TemporaryDirectory(prefix="engine-", dir=scratch_root)
    try:
        yield workspace.name
    finally:
        # Job accounting can reach zero while Windows is still releasing a
        # descendant's directory handle. Retry only this bounded cleanup race.
        until = time.monotonic() + 2
        while True:
            try:
                workspace.cleanup()
                break
            except PermissionError:
                if os.name != "nt" or time.monotonic() >= until:
                    raise
                time.sleep(.025)


class ProcessFence:
    def __init__(self, process, max_bytes):
        self.process, self.job = process, None
        if os.name != "nt":
            return
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)

        class Basic(ctypes.Structure):
            _fields_ = [("ProcessTime", ctypes.c_int64), ("JobTime", ctypes.c_int64),
                ("Flags", wintypes.DWORD), ("MinWorking", ctypes.c_size_t),
                ("MaxWorking", ctypes.c_size_t), ("ActiveProcesses", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("Priority", wintypes.DWORD),
                ("Scheduling", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]

        class Extended(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("IO", IO), ("ProcessMemory", ctypes.c_size_t),
                ("JobMemory", ctypes.c_size_t), ("PeakProcess", ctypes.c_size_t),
                ("PeakJob", ctypes.c_size_t)]

        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        self.kernel = kernel
        self.job = kernel.CreateJobObjectW(None, None)
        limits = Extended()
        limits.Basic.Flags = 0x2000 | 0x100 | 0x200  # kill on close, process and job memory limits
        limits.ProcessMemory = limits.JobMemory = max_bytes
        if (not self.job or not kernel.SetInformationJobObject(self.job, 9, ctypes.byref(limits),
                ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(self.job, int(process._handle))):
            self.close()
            raise MediaError("Unable to establish engine process resource fence")

    def resume(self):
        if os.name == "nt":
            native = ctypes.WinDLL("ntdll")
            native.NtResumeProcess.argtypes = [wintypes.HANDLE]
            native.NtResumeProcess.restype = ctypes.c_long
            if native.NtResumeProcess(int(self.process._handle)) != 0:
                raise MediaError("Unable to resume the fenced engine")

    def close(self):
        if self.job:
            self.kernel.TerminateJobObject(self.job, 1)
            class Accounting(ctypes.Structure):
                _fields_ = [("User", ctypes.c_int64), ("Kernel", ctypes.c_int64),
                    ("PeriodUser", ctypes.c_int64), ("PeriodKernel", ctypes.c_int64),
                    ("Faults", wintypes.DWORD), ("Processes", wintypes.DWORD),
                    ("Active", wintypes.DWORD), ("Terminated", wintypes.DWORD)]
            until = time.monotonic() + 5
            while time.monotonic() < until:
                accounting = Accounting()
                if (not self.kernel.QueryInformationJobObject(self.job, 1, ctypes.byref(accounting),
                        ctypes.sizeof(accounting), None) or accounting.Active == 0):
                    break
                time.sleep(.01)
            self.kernel.CloseHandle(self.job)
            self.job = None
        elif os.name != "nt":
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)


class SubprocessRepairEngine:
    def __init__(self, *, name, version, executable, script, executable_sha256, script_sha256,
                 scratch_root, weights_sha256=None, max_process_bytes=512 * 1024**2,
                 max_output_bytes=64 * 1024**2):
        self.name, self.version, self.weights_sha256 = name, version, weights_sha256
        self.executable, self.script = Path(executable).resolve(), Path(script).resolve()
        self.executable_sha256, self.script_sha256 = executable_sha256, script_sha256
        self.scratch_root = Path(scratch_root).resolve()
        if not self.scratch_root.is_dir() or not 64 * 1024**2 <= max_process_bytes <= 2 * 1024**3:
            raise ValueError("Engine needs an existing isolated scratch directory and bounded memory")
        if not 0 < max_output_bytes <= 512 * 1024**2:
            raise ValueError("Invalid engine output allowance")
        self.max_process_bytes, self.max_output_bytes = max_process_bytes, max_output_bytes

    def run(self, frames, removal_masks, *, deadline, cancelled):
        if (file_sha(self.executable) != self.executable_sha256
                or file_sha(self.script) != self.script_sha256):
            raise MediaError("Pinned engine runtime or entrypoint changed")
        if cancelled() or time.monotonic() >= deadline:
            raise MediaError("Engine cancelled before process launch")
        with engine_workspace(self.scratch_root) as directory:
            root = Path(directory)
            for index, (frame, mask) in enumerate(zip(frames, removal_masks, strict=True)):
                frame.save(root / f"in-{index}.png")
                mask.save(root / f"mask-{index}.png")
            publish_json(root / "request.json", {"revision": 1, "frames": len(frames),
                "width": frames[0].width, "height": frames[0].height, "output": "out-{index}.png"})
            launcher = Path(__file__).with_name("process_launcher.py")
            command = [str(self.executable), "-I", "-B", str(launcher), str(self.script), str(root),
                       str(self.max_process_bytes)]
            environment = {key: value for key, value in os.environ.items()
                           if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"}}
            # Arbitrary tool logs are neither evidence nor bounded output. Discard
            # them rather than letting inherited log handles pin a Windows tree.
            with open(os.devnull, "wb") as output:
                process = subprocess.Popen(command, cwd=root, env=environment, stdin=subprocess.PIPE,
                    stdout=output, stderr=subprocess.STDOUT, start_new_session=os.name != "nt",
                    # Suspend before Python/venv launchers can spawn an unfenced child.
                    creationflags=(subprocess.CREATE_NO_WINDOW | 0x4) if os.name == "nt" else 0)
                fence = None
                try:
                    fence = ProcessFence(process, self.max_process_bytes)
                    fence.resume()
                    process.stdin.write(b"go\n")
                    process.stdin.close()
                    while process.poll() is None:
                        if cancelled() or time.monotonic() >= deadline:
                            raise MediaError("Engine process cancelled or timed out")
                        if sum(p.stat().st_size for p in root.iterdir() if p.is_file()) > self.max_output_bytes:
                            raise MediaError("Engine process exceeded its workspace output allowance")
                        time.sleep(.02)
                    if process.returncode != 0:
                        raise MediaError("Engine process failed; retain evaluation receipt")
                    if sum(p.stat().st_size for p in root.iterdir() if p.is_file()) > self.max_output_bytes:
                        raise MediaError("Engine process exceeded its workspace output allowance")
                    result = []
                    for index in range(len(frames)):
                        path = root / f"out-{index}.png"
                        if path.is_symlink() or path.stat().st_size > self.max_output_bytes:
                            raise MediaError("Engine output escaped its declared file contract")
                        with Image.open(path) as image:
                            if image.size != frames[index].size or image.mode != "RGB":
                                raise MediaError("Engine output geometry or mode changed")
                            result.append(image.copy())
                    return result
                finally:
                    if fence is not None:
                        fence.close()
                    elif process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
