"""
Code Runner — Execute code directly. Docker IS the sandbox.

No Sandbox class, no strict_mode, no validate_code().
Safety boundary lives in Orchestrator's tool registration layer.

Supports: python, javascript, bash.
"""

import asyncio
import logging
import os
import shutil
import sys
import tempfile
from typing import Any

logger = logging.getLogger("tools.code_runner")

# Language → (interpreter, file suffix)
_RUNNERS: dict[str, tuple[str, str]] = {
    "python": (sys.executable, ".py"),
    "javascript": (shutil.which("node") or "node", ".js"),
    "bash": (shutil.which("bash") or "/bin/sh", ".sh"),
}


async def execute_code(
    code: str,
    language: str = "python",
    timeout: int = 30,
) -> dict[str, Any]:
    """Execute code in a subprocess.

    This is the agent's foundational capability — if specialized tools
    cannot solve a problem, the agent can always write code.

    Args:
        code: Source code to execute.
        language: One of "python", "javascript", "bash".
        timeout: Max execution time in seconds.

    Returns:
        {"success": bool, "stdout": str, "stderr": str, "returncode": int}
    """
    runner_info = _RUNNERS.get(language)
    if not runner_info:
        return {
            "success": False,
            "stdout": "",
            "stderr": f"Unsupported language: {language}. Supported: {list(_RUNNERS.keys())}",
            "returncode": -1,
        }

    interpreter, suffix = runner_info

    # Write code to temp file
    fd, temp_path = tempfile.mkstemp(suffix=suffix, prefix="crawl_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(code)

        # Make bash scripts executable
        if language == "bash":
            os.chmod(temp_path, 0o755)

        proc = await asyncio.create_subprocess_exec(
            interpreter,
            temp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            return {
                "success": proc.returncode == 0,
                "stdout": stdout.decode("utf-8", errors="replace") if stdout else "",
                "stderr": stderr.decode("utf-8", errors="replace") if stderr else "",
                "returncode": proc.returncode,
            }
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {
                "success": False,
                "stdout": "",
                "stderr": f"Execution timed out ({timeout}s)",
                "returncode": -1,
            }

    except Exception as e:
        return {
            "success": False,
            "stdout": "",
            "stderr": f"Failed to execute: {e}",
            "returncode": -1,
        }
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


async def execute_code_safe(
    code: str,
    language: str = "python",
    timeout: int = 30,
) -> dict[str, Any]:
    """Restricted mode for non-Docker environments.

    Only allows Python. Checks for dangerous patterns before execution.
    """
    if language != "python":
        return {
            "success": False,
            "stdout": "",
            "stderr": "Only Python is allowed in restricted mode",
            "returncode": -1,
        }

    import re
    dangerous = [
        (r"\bos\.system\b", "os.system"),
        (r"\bos\.popen\b", "os.popen"),
        (r"\bsubprocess\.", "subprocess"),
        (r"\b__import__\s*\(", "__import__"),
        (r"\bshutil\.rmtree\b", "shutil.rmtree"),
    ]
    for pattern, name in dangerous:
        if re.search(pattern, code):
            return {
                "success": False,
                "stdout": "",
                "stderr": f"Blocked: {name} is not allowed in restricted mode",
                "returncode": -1,
            }

    return await execute_code(code, "python", timeout)
