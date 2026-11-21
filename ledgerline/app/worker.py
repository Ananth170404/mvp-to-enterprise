"""The worker: takes jobs off the queue and runs them (guide Chapter 6).

    python -m app.worker            run until stopped (SIGTERM / Ctrl+C)
    python -m app.worker --check    exit 0 if the worker is alive (container health check)

The loop, and what each part protects against:

    receive (long poll, 20 s)       waiting costs nothing; an empty queue isn't busy-polled
      │
      ├─ heartbeat while running    a slow job keeps the message hidden from other
      │                             workers; a DEAD worker stops the heartbeat, and
      │                             SQS hands the job to someone else
      ├─ run the job (app/jobs.py)
      │
      ├─ success          → delete the message: done
      ├─ exception        → leave it: SQS delivers it again after the visibility
      │                     timeout; after 3 deliveries, to the dead-letter queue
      └─ poison message   → leave it too, logged as an error: it will end up in
                            the dead-letter queue, where a person can look at it

On SIGTERM (ECS stopping the task for a deploy or scale-in), the worker
finishes the job in hand and stops asking for more. ECS waits up to the task's
stopTimeout (120 s) before killing it; a job still running then is retried.

Every few minutes, the worker also sweeps for invoices whose extraction never
finished (app/maintenance.py).
"""

import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path

from app import jobs
from app.config import settings
from app.maintenance import recover_stuck_extractions
from app.observability import job_wait, jobs_processed, log_context

log = logging.getLogger("ledgerline.worker")

ALIVE_FILE = Path("/tmp/ledgerline-worker-alive")  # noqa: S108 (a heartbeat marker, not data)
SWEEP_EVERY_SECONDS = 300
WAIT_SECONDS = 20  # SQS long polling maximum


class Worker:
    def __init__(self, client, queue_url: str, visibility_seconds: int, stop: threading.Event | None = None):
        self.client = client
        self.queue_url = queue_url
        self.visibility = visibility_seconds
        self.stop = stop or threading.Event()
        self._last_sweep = 0.0

    def run_forever(self) -> None:
        log.info("Worker started", extra={"event": "worker_started", "queue": self.queue_url})
        while not self.stop.is_set():
            self.poll_once()
        log.info("Worker stopped", extra={"event": "worker_stopped"})

    def poll_once(self, wait_seconds: int = WAIT_SECONDS) -> int:
        """Receive at most one job and handle it. Returns how many were handled."""
        _touch_alive()
        self._sweep_if_due()
        response = self.client.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=1,  # one job at a time: extraction is slow and memory-hungry
            WaitTimeSeconds=wait_seconds,
            MessageAttributeNames=["All"],
            # The older name for MessageSystemAttributeNames: still supported by
            # SQS, and understood by local emulators that predate the new one.
            AttributeNames=["ApproximateReceiveCount", "SentTimestamp"],
        )
        messages = response.get("Messages", [])
        for message in messages:
            self.handle(message)
        return len(messages)

    def handle(self, message: dict) -> str:
        """Run one message's job. Returns the outcome: done | retry | poison."""
        system = message.get("Attributes", {})
        attempt = int(system.get("ApproximateReceiveCount", "1"))
        carrier = {k: v["StringValue"] for k, v in message.get("MessageAttributes", {}).items()
                   if v.get("DataType") == "String"}
        sent = jobs.sent_at(system)
        waited = max(0.0, time.time() - sent.timestamp()) if sent else None

        # Every log line during this job says which job it was.
        ctx = log_context()
        ctx.clear()
        ctx.update({"job_id": message["MessageId"], "job_attempt": attempt})
        log.info("Job started", extra={"event": "job_started", "job_waited_ms": round(waited * 1000) if waited else None})
        if waited is not None:
            job_wait.record(waited)

        beat = _Heartbeat(self.client, self.queue_url, message["ReceiptHandle"], self.visibility)
        beat.start()
        try:
            jobs.run(message["Body"], carrier, attempt=attempt)
        except jobs.PoisonMessage:
            log.exception("Poison message; leaving it for the dead-letter queue", extra={"event": "job_poison"})
            outcome = "poison"
        except Exception:
            log.exception("Job failed (attempt %d); it will be retried", attempt, extra={"event": "job_failed"})
            outcome = "retry"
        else:
            outcome = "done"
        finally:
            beat.stop()

        if outcome == "done":
            self.client.delete_message(QueueUrl=self.queue_url, ReceiptHandle=message["ReceiptHandle"])
        log.info("Job finished: %s", outcome, extra={"event": "job_finished", "outcome": outcome})
        jobs_processed.add(1, {"outcome": outcome})
        ctx.clear()
        return outcome

    def _sweep_if_due(self) -> None:
        if time.monotonic() - self._last_sweep < SWEEP_EVERY_SECONDS:
            return
        self._last_sweep = time.monotonic()
        try:
            recover_stuck_extractions()
        except Exception:
            log.exception("Stuck-extraction sweep failed; continuing")


class _Heartbeat:
    """While a job runs, keep pushing its visibility timeout forward, so no
    other worker picks it up. Stops when the job ends, or when the process dies,
    which is exactly when another worker should take over."""

    def __init__(self, client, queue_url: str, receipt: str, visibility: int):
        self.client, self.queue_url, self.receipt, self.visibility = client, queue_url, receipt, visibility
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._done.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._done.wait(self.visibility / 2):
            _touch_alive()
            try:
                self.client.change_message_visibility(
                    QueueUrl=self.queue_url, ReceiptHandle=self.receipt, VisibilityTimeout=self.visibility,
                )
            except Exception:
                log.warning("Could not extend the job's visibility timeout", exc_info=True)


def _touch_alive() -> None:
    try:
        ALIVE_FILE.touch()
    except OSError:
        pass


def is_alive(max_age_seconds: float) -> bool:
    try:
        return time.time() - ALIVE_FILE.stat().st_mtime < max_age_seconds
    except OSError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Ledgerline background worker")
    parser.add_argument("--check", action="store_true", help="exit 0 if a worker in this container is alive")
    args = parser.parse_args()
    if args.check:
        # The loop touches the file at least every 20 s, and every half-visibility during a job.
        sys.exit(0 if is_alive(max_age_seconds=settings.job_visibility_seconds + 60) else 1)

    from app.db import engine
    from app.observability import configure_logging, setup_sentry, setup_telemetry

    configure_logging(settings.log_format)
    setup_sentry(settings.sentry_dsn, environment=settings.app_env, release=settings.git_sha)
    setup_telemetry(engine, service_version=settings.git_sha, environment=settings.app_env)
    if settings.queue_backend != "sqs":
        sys.exit("The worker needs QUEUE_BACKEND=sqs (with QUEUE_BACKEND=inline, jobs run in the web app).")

    stop = threading.Event()

    def request_stop(signum, _frame):
        log.info("Stop requested (signal %d): finishing the current job", signum, extra={"event": "worker_stopping"})
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    Worker(jobs.sqs_client(), jobs.queue_url(), settings.job_visibility_seconds, stop).run_forever()


if __name__ == "__main__":
    main()
