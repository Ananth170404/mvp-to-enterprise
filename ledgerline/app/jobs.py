"""Background jobs: the web app puts a message on a queue; app.worker does the work.

Until Chapter 6, extraction ran as a FastAPI background task inside the web
process. A deploy or crash in the middle lost it, and a slow model call took
a web server's memory and CPU while it waited. Now:

    web app ── enqueue_extraction() ──> SQS queue ──> app.worker ──> extract_invoice()
                                            │
                                            └── after 3 failed attempts ──> dead-letter queue

A job message is small JSON: what to do, to which record, and the version of
the record when it was asked for:

    {"type": "extract_invoice", "tenant_id": "…", "invoice_id": "…", "version": "2026-11-14T06:35:12.123456"}

The tenant scopes the worker's database access (row-level security, Chapter 7).

The version (the invoice's status_changed_at) makes a job safe to deliver
twice, which SQS can do: if the invoice has changed since the job was queued,
someone already did the work, and the job is skipped (app/extraction.py).

Two backends, chosen by QUEUE_BACKEND:
  sqs     the real thing (deployed environments; ElasticMQ in local Docker)
  inline  runs the job immediately in the calling process; for tests and for
          running the app without Docker. Same message, same handler, so
          tests exercise the code path the worker uses.
"""

import json
import logging
from datetime import UTC, datetime
from functools import cache
from urllib.parse import urlsplit

from opentelemetry import context as otel_context
from opentelemetry import propagate

from app.config import settings
from app.models import Invoice

log = logging.getLogger("ledgerline.jobs")

EXTRACT_INVOICE = "extract_invoice"


class PoisonMessage(Exception):
    """A message that can never succeed (unreadable, unknown type). Not retried
    usefully: it goes to the dead-letter queue for a person to look at."""


def version_of(invoice: Invoice) -> str:
    """The invoice's version, as text that survives a round trip through JSON
    and through any database (SQLite drops the time zone; Postgres keeps it)."""
    changed = invoice.status_changed_at
    if changed is None:
        return ""
    if changed.tzinfo is not None:
        changed = changed.astimezone(UTC).replace(tzinfo=None)
    return changed.isoformat()


def enqueue_extraction(invoice: Invoice) -> None:
    """Ask for this invoice to be extracted. Call AFTER the transaction that
    created or changed it has committed: a worker must be able to see it."""
    send({"type": EXTRACT_INVOICE, "tenant_id": invoice.tenant_id, "invoice_id": invoice.id,
          "version": version_of(invoice)})


def send(job: dict) -> None:
    # Trace context travels with the message (W3C traceparent), so the
    # worker's trace links back to the request that asked (Chapter 3).
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    body = json.dumps(job)
    if settings.queue_backend == "inline":
        run(body, carrier, attempt=1)
        return
    attributes = {k: {"DataType": "String", "StringValue": v} for k, v in carrier.items()}
    sqs_client().send_message(QueueUrl=queue_url(), MessageBody=body, MessageAttributes=attributes)
    log.info("Job queued: %s", job["type"], extra={"event": "job_queued", "job_type": job["type"],
                                                   "invoice_id": job.get("invoice_id")})


def run(body: str, carrier: dict[str, str], attempt: int) -> None:
    """Run one job. Raises PoisonMessage for messages that can never work, and
    lets any other exception through: the worker leaves the message on the
    queue, and SQS delivers it again later."""
    try:
        job = json.loads(body)
        job_type = job["type"]
    except (ValueError, KeyError, TypeError) as exc:
        raise PoisonMessage(f"Unreadable job message: {exc}") from exc

    if job_type == EXTRACT_INVOICE:
        from app.extraction import extract_invoice  # imported here: extraction imports the models, the LLM, …

        token = otel_context.attach(propagate.extract(carrier))
        try:
            extract_invoice(
                job["invoice_id"],
                expected_version=job.get("version"),
                tenant_id=job.get("tenant_id"),
                # A second delivery means an earlier attempt stopped part-way
                # (worker killed, crashed). Allow taking over its "extracting".
                reclaim_after_seconds=settings.job_visibility_seconds if attempt > 1 else None,
            )
        finally:
            otel_context.detach(token)
    else:
        raise PoisonMessage(f"Unknown job type {job_type!r}")


# ---------------------------------------------------------------- SQS


@cache
def sqs_client():
    import boto3

    if settings.sqs_endpoint_url:  # local ElasticMQ: any credentials are accepted
        return boto3.client("sqs", endpoint_url=settings.sqs_endpoint_url, region_name="us-east-1",
                            aws_access_key_id="local", aws_secret_access_key="local")  # noqa: S106 (not a secret)
    return boto3.client("sqs", region_name=settings.aws_region)


@cache
def queue_url() -> str:
    """The queue's URL. In local development the queues are created on first
    use (with their dead-letter queue); in AWS, Terraform creates them."""
    url = settings.job_queue_url or ""
    if settings.sqs_endpoint_url and not settings.is_deployed:
        created = create_queues(sqs_client(), urlsplit(url).path.rsplit("/", 1)[-1], settings.job_visibility_seconds)
        return _via_endpoint(created)
    return url


def _via_endpoint(url: str) -> str:
    """Local servers may answer with their own idea of their host name
    ("localhost"), which is wrong from inside another container. Keep the path,
    use the address we reached them at."""
    if not settings.sqs_endpoint_url:
        return url
    return settings.sqs_endpoint_url.rstrip("/") + urlsplit(url).path


def create_queues(client, name: str, visibility_seconds: int) -> str:
    """The job queue and its dead-letter queue, for local development and tests."""
    dlq_url = client.create_queue(QueueName=f"{name}-dlq")["QueueUrl"]
    dlq_arn = client.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    url = client.create_queue(
        QueueName=name,
        Attributes={
            "VisibilityTimeout": str(visibility_seconds),
            "ReceiveMessageWaitTimeSeconds": "20",
            # Same rule as in AWS (infra/modules/environment/jobs.tf): three
            # deliveries, then the message is moved to the dead-letter queue.
            "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "3"}),
        },
    )["QueueUrl"]
    return url


def sent_at(attributes: dict) -> datetime | None:
    millis = attributes.get("SentTimestamp")
    return datetime.fromtimestamp(int(millis) / 1000, UTC) if millis else None


# ---------------------------------------------------------------- command line


def _dlq_url(client, url: str) -> str | None:
    policy = client.get_queue_attributes(QueueUrl=url, AttributeNames=["RedrivePolicy"])["Attributes"]
    if "RedrivePolicy" not in policy:
        return None
    name = json.loads(policy["RedrivePolicy"])["deadLetterTargetArn"].rsplit(":", 1)[-1]
    return _via_endpoint(client.get_queue_url(QueueName=name)["QueueUrl"])


def status() -> None:
    client, url = sqs_client(), queue_url()
    for label, q in (("jobs", url), ("dead letters", _dlq_url(client, url))):
        if q is None:
            continue
        a = client.get_queue_attributes(QueueUrl=q, AttributeNames=["All"])["Attributes"]
        print(f"{label:<13} waiting {a['ApproximateNumberOfMessages']:>5}   "
              f"in progress {a['ApproximateNumberOfMessagesNotVisible']:>5}")


def redrive() -> None:
    """Move every message from the dead-letter queue back to the job queue, once
    the cause is fixed. (In AWS, `aws sqs start-message-move-task` does the same.)"""
    client, url = sqs_client(), queue_url()
    dlq = _dlq_url(client, url)
    moved = 0
    while dlq:
        messages = client.receive_message(QueueUrl=dlq, MaxNumberOfMessages=10, WaitTimeSeconds=0,
                                          MessageAttributeNames=["All"]).get("Messages", [])
        if not messages:
            break
        for m in messages:
            client.send_message(QueueUrl=url, MessageBody=m["Body"], MessageAttributes=m.get("MessageAttributes", {}))
            client.delete_message(QueueUrl=dlq, ReceiptHandle=m["ReceiptHandle"])
            moved += 1
    print(f"Moved {moved} message(s) back to the job queue")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Ledgerline job queue")
    parser.add_argument("command", choices=["status", "redrive"])
    {"status": status, "redrive": redrive}[parser.parse_args().command]()
