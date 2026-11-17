"""
Shared boto3 clients, built only when first used and then reused.

A deployment without AWS (Azure: CHAT_STORE=postgres, JOB_QUEUE=postgres,
S3_ENDPOINT_URL set, BEDROCK_ENABLED=false) never builds an AWS client, so it
never looks for AWS credentials. That matters: boto3's credential chain falls
through to the EC2 metadata endpoint, which off AWS hangs each call for seconds
(FojiApi saw ~6.5 s per request before its SQS client went lazy).

boto3 clients are thread-safe; route code calls them through asyncio.to_thread.
"""

from functools import lru_cache

from botocore.config import Config

from app.core.config import settings

# Short timeouts and standard retries: these are small reads/writes on the
# request path, and a hung call must not hold a request for a minute.
BOTO_CONFIG = Config(
    max_pool_connections=32,
    retries={"mode": "standard", "total_max_attempts": 3},
    connect_timeout=3,
    read_timeout=10,
)

# S3-compatible stores (Cloudflare R2): path-style addressing, and checksums
# only where the API requires them (botocore >= 1.36 adds CRC32 headers to
# every put by default, which S3-compatible stores don't all accept).
_S3_COMPATIBLE_CONFIG = BOTO_CONFIG.merge(Config(
    signature_version="s3v4",
    s3={"addressing_style": "path"},
    request_checksum_calculation="when_required",
    response_checksum_validation="when_required",
))


def aws_credentials() -> dict:
    """Explicit keys when configured, else boto3's default chain (task role)."""
    kwargs = {"region_name": settings.AWS_REGION}
    if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
        kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
        kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
    return kwargs


def s3_client_kwargs() -> dict:
    """
    How the S3 client is built. With S3_ENDPOINT_URL: that endpoint, region
    "auto" and the S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY keys, never the AWS
    ones. Without it: AWS S3 in S3_REGION, as always.
    """
    if settings.S3_ENDPOINT_URL:
        return {
            "endpoint_url": settings.S3_ENDPOINT_URL,
            "region_name": "auto",
            "aws_access_key_id": settings.S3_ACCESS_KEY_ID or None,
            "aws_secret_access_key": settings.S3_SECRET_ACCESS_KEY or None,
            "config": _S3_COMPATIBLE_CONFIG,
        }
    kwargs = aws_credentials()
    kwargs["region_name"] = settings.S3_REGION or settings.AWS_REGION or "us-east-2"
    kwargs["config"] = BOTO_CONFIG
    return kwargs


def s3_object_url(bucket: str, key: str) -> str:
    """The URL stored next to an uploaded object (Files.BlobUrl and friends)."""
    if settings.S3_ENDPOINT_URL:
        return f"{settings.S3_ENDPOINT_URL.rstrip('/')}/{bucket}/{key}"
    region = settings.S3_REGION or settings.AWS_REGION or "us-east-2"
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


@lru_cache
def s3_client():
    import boto3

    return boto3.client("s3", **s3_client_kwargs())


@lru_cache
def sqs_client():
    import boto3

    return boto3.client("sqs", config=BOTO_CONFIG, **aws_credentials())


@lru_cache
def dynamodb_resource():
    import boto3

    return boto3.resource("dynamodb", config=BOTO_CONFIG, **aws_credentials())
