"""Chapter 6: background jobs through a real queue API (moto's in-memory SQS).

What these pin down is the contract between the web app, the queue and the
worker: a job is deleted only when it succeeded; failures come back and end in
the dead-letter queue; a job delivered twice does its work once; and a job
whose worker died part-way is taken over by the retry.
"""

import dataclasses
from datetime import timedelta

import boto3
import pytest
from moto import mock_aws
from sqlalchemy import func, select

from app import jobs
from app.models import ExtractionRun, Invoice, utcnow
from app.worker import Worker
from tests.helpers import client_for, first, upload


@pytest.fixture()
def queue(monkeypatch):
    """A real SQS API (moto), wired in as the app's queue. Visibility 0 makes a
    failed job visible again immediately, so retries don't need waiting for."""
    with mock_aws():
        client = boto3.client("sqs", region_name="ap-south-1")
        url = jobs.create_queues(client, "extraction", visibility_seconds=0)
        dlq_url = client.get_queue_url(QueueName="extraction-dlq")["QueueUrl"]
        monkeypatch.setattr(jobs, "settings", dataclasses.replace(jobs.settings, queue_backend="sqs", job_queue_url=url))
        monkeypatch.setattr(jobs, "sqs_client", lambda: client)
        monkeypatch.setattr(jobs, "queue_url", lambda: url)
        worker = Worker(client, url, visibility_seconds=120)
        worker._last_sweep = float("inf")  # the sweep has its own tests
        yield client, url, dlq_url, worker


def _count(client, url) -> int:
    attrs = client.get_queue_attributes(
        QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
    )["Attributes"]
    return int(attrs["ApproximateNumberOfMessages"]) + int(attrs["ApproximateNumberOfMessagesNotVisible"])


def _invoice_id(response) -> str:
    return response.headers["location"].split("/invoices/")[1].split("?")[0]


def _runs(session, invoice_id) -> int:
    return session.scalar(select(func.count()).select_from(ExtractionRun).where(ExtractionRun.invoice_id == invoice_id))


def test_upload_queues_a_job_and_the_worker_runs_it(db, manifest, queue):
    session, users = db
    client, url, _, worker = queue

    invoice_id = _invoice_id(upload(client_for(users["clerk"]), first(manifest, "clean")))
    session.expire_all()
    assert session.get(Invoice, invoice_id).status == "uploaded"  # the request didn't do the work
    assert _count(client, url) == 1

    assert worker.poll_once(wait_seconds=0) == 1
    session.expire_all()
    assert session.get(Invoice, invoice_id).status == "needs_review"
    assert _count(client, url) == 0  # deleted after success


def test_a_job_delivered_twice_does_the_work_once(db, manifest, queue):
    session, users = db
    client, url, _, worker = queue
    invoice_id = _invoice_id(upload(client_for(users["clerk"]), first(manifest, "clean")))
    body = client.receive_message(QueueUrl=url)["Messages"][0]["Body"]
    client.send_message(QueueUrl=url, MessageBody=body)  # the duplicate

    worker.poll_once(wait_seconds=0)
    worker.poll_once(wait_seconds=0)
    worker.poll_once(wait_seconds=0)
    session.expire_all()
    assert session.get(Invoice, invoice_id).status == "needs_review"
    assert _runs(session, invoice_id) == 1  # one LLM call, not two
    assert _count(client, url) == 0


def test_a_retry_takes_over_from_a_worker_that_died(db, manifest, queue):
    session, users = db
    client, url, _, worker = queue
    invoice_id = _invoice_id(upload(client_for(users["clerk"]), first(manifest, "clean")))

    # A worker received the job, marked the invoice "extracting", then died:
    # no heartbeat, no delete. Its claim is older than the visibility timeout.
    client.receive_message(QueueUrl=url)
    invoice = session.get(Invoice, invoice_id)
    invoice.status, invoice.status_changed_at = "extracting", utcnow() - timedelta(minutes=5)
    session.commit()

    assert worker.poll_once(wait_seconds=0) == 1  # second delivery
    session.expire_all()
    assert session.get(Invoice, invoice_id).status == "needs_review"


def test_a_first_delivery_never_takes_over_a_running_extraction(db, manifest, queue):
    session, users = db
    client, url, _, worker = queue
    invoice_id = _invoice_id(upload(client_for(users["clerk"]), first(manifest, "clean")))
    invoice = session.get(Invoice, invoice_id)
    invoice.status, invoice.status_changed_at = "extracting", utcnow() - timedelta(minutes=5)
    session.commit()

    worker.poll_once(wait_seconds=0)  # attempt 1: someone else may still be working on it
    session.expire_all()
    assert session.get(Invoice, invoice_id).status == "extracting"


def test_failing_jobs_are_retried_then_dead_lettered(db, manifest, queue, monkeypatch):
    session, users = db
    client, url, dlq_url, worker = queue

    def database_down(*args, **kwargs):
        raise ConnectionError("database is down")

    monkeypatch.setattr("app.extraction._extract", database_down)
    upload(client_for(users["clerk"]), first(manifest, "clean"))

    outcomes = [worker.handle(m) for _ in range(3) for m in client.receive_message(
        QueueUrl=url, AttributeNames=["ApproximateReceiveCount"])["Messages"]]
    assert outcomes == ["retry", "retry", "retry"]
    assert client.receive_message(QueueUrl=url, WaitTimeSeconds=0).get("Messages", []) == []  # moved on the next receive
    assert _count(client, dlq_url) == 1


def test_unreadable_messages_are_left_for_the_dead_letter_queue(db, queue):
    client, url, _, worker = queue
    client.send_message(QueueUrl=url, MessageBody="this is not JSON")
    message = client.receive_message(QueueUrl=url)["Messages"][0]
    assert worker.handle(message) == "poison"
    assert _count(client, url) == 1  # not deleted


def test_never_started_extractions_are_failed_by_the_sweep(db, manifest):
    from app.maintenance import recover_stuck_extractions

    session, users = db
    invoice_id = _invoice_id(upload(client_for(users["clerk"]), first(manifest, "clean")))
    invoice = session.get(Invoice, invoice_id)
    invoice.status, invoice.status_changed_at = "uploaded", utcnow() - timedelta(minutes=30)
    session.commit()
    assert recover_stuck_extractions(older_than_minutes=10) == []  # could still be waiting in a backlog

    invoice.status_changed_at = utcnow() - timedelta(hours=2)
    session.commit()
    assert recover_stuck_extractions(older_than_minutes=10) == [invoice_id]
    session.expire_all()
    assert session.get(Invoice, invoice_id).status == "extraction_failed"
