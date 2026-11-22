"""Launch local app processes; Debugpy's subProcess option debugs each Python child.

Zed starts Compose through the profile's build task before running this script.
Stopping the debug session also kills Vite because all children stay in the
debugger's process group. Compose services remain running for the next session.
"""

import asyncio
import os
import signal
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"


async def main() -> int:
    processes: list[tuple[str, asyncio.subprocess.Process]] = []
    waiters = {}
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stopped.set)
    stop_task = asyncio.create_task(stopped.wait())
    env = os.environ.copy()
    env.setdefault("TEMPORAL_ADDRESS", "localhost:7233")
    env["PYTHONUNBUFFERED"] = "1"

    try:
        services = [
            ("API", BACKEND, [sys.executable, "-m", "uvicorn", "app.api.main:app",
                              "--host", "127.0.0.1", "--port", "8000"]),
            ("Temporal worker", BACKEND, [sys.executable, "-m", "app.temporal.worker"]),
            ("Codegen worker", BACKEND, [sys.executable, "-m", "app.codegen.worker"]),
            # It pulls the pinned images of docker_agents.PREPULLED_IMAGES (ensure_images)
            # into the host's Docker daemon in the background when it starts, as the
            # sandbox worker does into the sandbox's daemon in a deployment.
            ("Sandbox worker", BACKEND,
             [sys.executable, "-m", "app.agent_workflows.worker"]),
            # Launch Vite directly so graceful shutdown does not leave npm's child alive.
            ("Frontend", ROOT / "frontend",
             ["node", str(ROOT / "frontend/node_modules/vite/bin/vite.js"), "--strictPort"]),
        ]
        for name, cwd, command in services:
            # Keep the debugger's process group so Stop also kills descendants.
            process = await asyncio.create_subprocess_exec(*command, cwd=cwd, env=env)
            processes.append((name, process))
            print(f"Started {name} (PID {process.pid})", flush=True)

        print("Frontend: http://localhost:5173 | API: http://127.0.0.1:8000/docs | "
              "Temporal UI: http://localhost:8080", flush=True)
        waiters = {asyncio.create_task(process.wait()): name for name, process in processes}
        done, _ = await asyncio.wait([*waiters, stop_task],
                                     return_when=asyncio.FIRST_COMPLETED)
        if stop_task in done:
            return 0
        finished = next(task for task in waiters if task in done)
        code = finished.result()
        print(f"{waiters[finished]} exited with code {code}; stopping app processes.",
              file=sys.stderr, flush=True)
        return code or 1
    except asyncio.CancelledError:
        return 0
    except OSError as exc:
        print(f"Could not start stack: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
        stop_task.cancel()
        for _, process in processes:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
        try:
            await asyncio.wait_for(asyncio.gather(*(process.wait() for _, process in processes)),
                                   timeout=5)
        except TimeoutError:
            for _, process in processes:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
            await asyncio.gather(*(process.wait() for _, process in processes))
        await asyncio.gather(*waiters, stop_task, return_exceptions=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
