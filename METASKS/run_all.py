from __future__ import annotations

import asyncio
import os
import signal
import sys
from typing import Optional


async def _stream_output(prefix: str, stream: asyncio.StreamReader) -> None:
    while True:
        line = await stream.readline()
        if not line:
            break
        try:
            text = line.decode(errors="ignore").rstrip()
        except Exception:
            text = str(line).rstrip()
        print(f"[{prefix}] {text}")


async def _run_process(prefix: str, cmd: list[str], cwd: Optional[str] = None, env: Optional[dict[str, str]] = None) -> int:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=env or os.environ.copy(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout and proc.stderr
    await asyncio.gather(_stream_output(prefix, proc.stdout), _stream_output(prefix, proc.stderr))
    return await proc.wait()


async def main() -> None:
    python = sys.executable
    pkg_dir = os.path.abspath(os.path.dirname(__file__))
    repo_root = os.path.abspath(os.path.join(pkg_dir, os.pardir))

    # Prefer zsteady.py inside METASKS if present; otherwise fall back to repo root
    zsteady_inside = os.path.join(pkg_dir, "zsteady.py")
    if os.path.exists(zsteady_inside):
        zsteady_cmd = [python, zsteady_inside]
        zsteady_cwd = pkg_dir
    else:
        zsteady_cmd = [python, os.path.join(repo_root, "zsteady.py")]
        zsteady_cwd = repo_root

    # Processes to launch: METASKS.bot and zsteady.py (kept independent)
    print("[LAUNCH] Starting METASKS.bot and zsteady...")
    task_metasks = asyncio.create_task(_run_process("METASKS", [python, "-m", "METASKS.bot"], cwd=repo_root))
    task_zsteady = asyncio.create_task(_run_process("ZSTEADY", zsteady_cmd, cwd=zsteady_cwd))
    tasks = [task_metasks, task_zsteady]

    # Graceful shutdown on Ctrl+C
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _handle_sig(*_args):  # noqa: ANN001
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_sig)
        except NotImplementedError:
            # Windows without Proactor might not support add_signal_handler
            pass

    # Exit early if any child terminates (crash or normal)
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for d in done:
        try:
            code = d.result()
        except Exception as exc:
            print(f"[LAUNCH] Child task crashed: {exc}")
            code = -1
        print(f"[LAUNCH] A child process exited with code {code}. Shutting down.")
    for p in pending:
        p.cancel()
        try:
            await p
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


