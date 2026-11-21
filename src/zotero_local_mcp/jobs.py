"""Long tool calls: after a wait, return a job id and keep the work running in the server.

A client that stops waiting (a tool timeout) then still gets the result with get_job, and a
write that is half done is not started a second time.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any

KEEP_SECONDS = 3600  # finished jobs are forgotten after this


def wait_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("ZOTERO_JOB_WAIT", "45")))
    except ValueError:
        return 45.0


class Jobs:
    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}

    async def run(self, tool: str, start: Callable[[], Awaitable[Any]], wait: float | None = None,
                  call: str = "") -> Any:
        """Run start() as a job. call identifies the arguments: while a job of the same tool and
        call runs, a new call does not start the work again but gets that job's id. This covers a
        client that stopped waiting (Esc, a client timeout) and then sends the same call again."""
        self._prune()
        for job_id, job in self._jobs.items():
            if job["tool"] == tool and job["call"] == call and not job["task"].done():
                return self._running(job_id, tool, "The same call is still running since "
                                     f"{job['started_at']}. Call get_job with this job_id for the result.")
        task = asyncio.ensure_future(start())
        # A failure in a job that nobody asks for must not log "exception was never retrieved".
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        # Register before the wait, so that a call whose client stops waiting is still known.
        job_id = "job-" + secrets.token_hex(4)
        job = {"tool": tool, "call": call, "task": task, "started": time.monotonic(),
               "started_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "finished": None}
        self._jobs[job_id] = job
        task.add_done_callback(lambda _t: job.update(finished=time.monotonic()))
        seconds = wait_seconds() if wait is None else wait
        done, _ = await asyncio.wait({task}, timeout=seconds)
        if task in done:
            del self._jobs[job_id]
            return task.result()
        return self._running(job_id, tool, f"Still working after {int(seconds)} s. Call get_job with this "
                             "job_id for the result. Do not call the tool again: the work goes on in the server.")

    @staticmethod
    def _running(job_id: str, tool: str, message: str) -> dict:
        return {"status": "running", "job_id": job_id, "tool": tool, "message": message}

    def get(self, job_id: str) -> dict:
        job = self._jobs.get(job_id)
        if job is None:
            raise LookupError(f"No job {job_id}. Jobs are kept for one hour after they end, "
                              "and a restart of the server forgets them.")
        task: asyncio.Task = job["task"]
        out: dict[str, Any] = {"job_id": job_id, "tool": job["tool"], "started": job["started_at"]}
        if not task.done():
            out.update(status="running", seconds=int(time.monotonic() - job["started"]))
        elif task.cancelled():
            out.update(status="failed", error="The job was cancelled.")
        elif task.exception() is not None:
            exc = task.exception()
            out.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        else:
            out.update(status="done", result=task.result())
        return out

    def _prune(self) -> None:
        now = time.monotonic()
        for jid in [j for j, v in self._jobs.items() if v["finished"] and now - v["finished"] > KEEP_SECONDS]:
            del self._jobs[jid]
