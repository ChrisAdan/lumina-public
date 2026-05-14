"""Python code execution in the sandboxed python-runner container.

The python-runner service runs python:3.11-slim with network_mode: none and a
shared ./sandbox volume. Code is written to the sandbox, executed via docker
exec, then cleaned up. The lumina-api container needs the Docker socket mounted
(/var/run/docker.sock) and ./sandbox:/app/sandbox to share files with the runner.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid

log = logging.getLogger(__name__)

CONTAINER_NAME = os.getenv("PYTHON_RUNNER_CONTAINER", "lumina-python-runner")
SANDBOX_HOST_DIR = os.getenv("PYTHON_SANDBOX_DIR", "/app/sandbox")
TIMEOUT_SECONDS = int(os.getenv("PYTHON_RUNNER_TIMEOUT", "15"))


async def run_python(code: str) -> dict:
    """Execute Python code in the sandboxed container.

    Returns {stdout, stderr, exit_code}. On timeout returns exit_code=-1.
    On container-not-found returns an error dict so the model can recover.
    """
    run_id = uuid.uuid4().hex[:8]
    script_name = f"run_{run_id}.py"
    host_script_path = os.path.join(SANDBOX_HOST_DIR, script_name)
    container_script_path = f"/app/{script_name}"

    try:
        os.makedirs(SANDBOX_HOST_DIR, exist_ok=True)
        with open(host_script_path, "w", encoding="utf-8") as f:
            f.write(code)

        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", CONTAINER_NAME, "python", container_script_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return {
                "stdout": "",
                "stderr": f"Execution timed out after {TIMEOUT_SECONDS}s",
                "exit_code": -1,
            }

        return {
            "stdout": stdout_b.decode("utf-8", errors="replace")[:4000],
            "stderr": stderr_b.decode("utf-8", errors="replace")[:1000],
            "exit_code": proc.returncode,
        }

    except FileNotFoundError:
        return {"error": f"docker not found — is the socket mounted? container={CONTAINER_NAME}"}
    except Exception as e:
        log.warning("python_runner error: %s", e)
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        try:
            os.unlink(host_script_path)
        except OSError:
            pass
