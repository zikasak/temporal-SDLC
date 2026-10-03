"""Launch local app processes; Debugpy's subProcess option debugs each Python child.

Zed starts Compose through the profile's build task before running this script.
Stopping the debug session also kills Vite because all children stay in the
debugger's process group. Compose services remain running for the next session.
"""

import os
from pathlib import Path
import signal
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"


def main() -> int:
    processes: list[tuple[str, subprocess.Popen]] = []
    env = os.environ.copy()
    env.setdefault("TEMPORAL_ADDRESS", "localhost:7233")
    env["PYTHONUNBUFFERED"] = "1"

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        services = [
            ("API", BACKEND, [sys.executable, "-m", "uvicorn", "app.api.main:app",
                              "--host", "127.0.0.1", "--port", "8000"]),
            ("Temporal worker", BACKEND, [sys.executable, "-m", "app.temporal.worker"]),
            ("Codegen worker", BACKEND, [sys.executable, "-m", "app.codegen.worker"]),
            ("Implementation worker", BACKEND,
             [sys.executable, "-m", "app.implementation.worker"]),
            ("Test generation worker", BACKEND,
             [sys.executable, "-m", "app.test_generation.worker"]),
            ("Verification worker", BACKEND,
             [sys.executable, "-m", "app.verification.worker"]),
            # Launch Vite directly so graceful shutdown does not leave npm's child alive.
            ("Frontend", ROOT / "frontend",
             ["node", str(ROOT / "frontend/node_modules/vite/bin/vite.js"), "--strictPort"]),
        ]
        for name, cwd, command in services:
            # Keep the debugger's process group so Stop also kills descendants.
            process = subprocess.Popen(command, cwd=cwd, env=env)
            processes.append((name, process))
            print(f"Started {name} (PID {process.pid})", flush=True)

        print("Frontend: http://localhost:5173 | API: http://127.0.0.1:8000/docs | "
              "Temporal UI: http://localhost:8080", flush=True)
        while True:
            for name, process in processes:
                code = process.poll()
                if code is not None:
                    print(f"{name} exited with code {code}; stopping app processes.",
                          file=sys.stderr, flush=True)
                    return code or 1
            time.sleep(0.25)
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        print(f"Could not start stack: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        for _, process in processes:
            if process.poll() is None:
                process.terminate()
        deadline = time.monotonic() + 5
        for _, process in processes:
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    sys.exit(main())
