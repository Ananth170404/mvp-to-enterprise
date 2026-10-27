from datetime import date
from decimal import Decimal

import pytest

from app import gstin
from app.config import Settings, validate
from app.llm.pricing import cost_usd
from app.money import fmt, to_minor


def test_money_never_uses_floats():
    assert to_minor("1,234.50", "INR") == 123450
    assert to_minor("0.1", "INR") + to_minor("0.2", "INR") == to_minor("0.3", "INR")
    assert to_minor("10.005", "INR") == 1001  # explicit half-up rounding
    assert to_minor("500", "JPY") == 500
    with pytest.raises(TypeError):
        to_minor(0.1, "INR")
    assert fmt(123450, "INR") == "INR 1,234.50"


def test_gstin_checksum_catches_misread_characters():
    assert gstin.is_valid("27AAPFU0939F1ZV")
    assert not gstin.is_valid("27AAPFU0939F1ZX")  # wrong check character
    assert not gstin.is_valid("27AAPFU0938F1ZV")  # one digit misread
    assert not gstin.is_valid("not-a-gstin")


def test_pricing_follows_effective_dates():
    before = cost_usd("gemini-3.8-flash", 1_000_000, 1_000_000, date(2026, 12, 31))
    after = cost_usd("gemini-3.8-flash", 1_000_000, 1_000_000, date(2027, 1, 1))
    assert before == Decimal("4.500000")
    assert after == Decimal("9.000000")
    assert cost_usd("some-unknown-model", 10, 10, date(2026, 10, 19)) is None


def _settings(**overrides) -> Settings:
    base = dict(
        app_env="production", database_url="x", storage_backend="s3", storage_dir=None, s3_bucket="b",
        aws_region="ap-south-1", llm_provider="gemini", llm_model="m", gemini_api_key="k", dev_login=False,
        site_password=None, max_upload_bytes=1, fake_llm_manifest=None, stuck_after_minutes=10,
    )
    base.update(overrides)
    return Settings(**base)


def test_production_refuses_dangerous_config():
    validate(_settings())  # a sane production config is fine
    with pytest.raises(RuntimeError, match="DEV_LOGIN"):
        validate(_settings(dev_login=True))
    with pytest.raises(RuntimeError, match="fake"):
        validate(_settings(llm_provider="fake"))
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        validate(_settings(gemini_api_key=None))


def test_deployed_environments_refuse_unsafe_shortcuts():
    validate(_settings(app_env="staging", dev_login=True, site_password="pw", llm_provider="fake"))
    with pytest.raises(RuntimeError, match="SITE_PASSWORD"):
        validate(_settings(app_env="staging", dev_login=True))
    with pytest.raises(RuntimeError, match="STORAGE_BACKEND=s3"):
        validate(_settings(app_env="staging", storage_backend="local"))
    with pytest.raises(RuntimeError, match="APP_ENV"):
        validate(_settings(app_env="prod"))
    validate(_settings(app_env="development", storage_backend="local", llm_provider="fake", dev_login=True))


def test_s3_storage_round_trip():
    import boto3
    from moto import mock_aws

    from app.storage import S3Storage

    with mock_aws():
        client = boto3.client("s3", region_name="ap-south-1")
        client.create_bucket(Bucket="ll-test", CreateBucketConfiguration={"LocationConstraint": "ap-south-1"})
        store = S3Storage("ll-test", client=client)
        store.put("tenants/t1/documents/abc.pdf", b"%PDF-1.4 hello", "application/pdf")
        assert store.get("tenants/t1/documents/abc.pdf") == b"%PDF-1.4 hello"
        assert store.exists("tenants/t1/documents/abc.pdf")
        assert not store.exists("tenants/t1/documents/missing.pdf")
        head = client.head_object(Bucket="ll-test", Key="tenants/t1/documents/abc.pdf")
        assert head["ServerSideEncryption"] == "AES256"
