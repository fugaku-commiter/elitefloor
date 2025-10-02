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
    tasks = [
        asyncio.create_task(_run_process("METASKS", [python, "-m", "METASKS.bot"], cwd=repo_root)),
        asyncio.create_task(_run_process("ZSTEADY", zsteady_cmd, cwd=zsteady_cwd)),
    ]

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

    await stop.wait()
    # Cancel tasks (subprocesses will end when parent exits)
    for t in tasks:
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


