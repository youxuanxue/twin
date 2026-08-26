from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class ProcessResult:
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool


class ProcessRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: float,
        input_text: str | None = None,
    ) -> ProcessResult:
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            env=environment,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(input_text, timeout=timeout_seconds)
            return ProcessResult(
                stdout=stdout,
                stderr=stderr,
                returncode=process.returncode,
                timed_out=False,
            )
        except subprocess.TimeoutExpired:
            self._terminate_process_group(process)
            stdout, stderr = process.communicate()
            return ProcessResult(
                stdout=stdout,
                stderr=stderr,
                returncode=process.returncode if process.returncode is not None else -signal.SIGKILL,
                timed_out=True,
            )

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen[str]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 0.25
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
