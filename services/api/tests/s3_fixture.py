"""A moto S3 server standing in for Backblaze B2 (versioned bucket, presigned URLs, multipart)."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from contextlib import contextmanager

BUCKET = "datacourt-test"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def moto_s3(cors_origin: str | None = None) -> Iterator[str]:
    """Start a local S3 endpoint with a versioned bucket (B2 buckets keep versions); yields its URL."""
    import boto3
    from moto.server import ThreadedMotoServer

    port = _free_port()
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    url = f"http://127.0.0.1:{port}"
    client = boto3.client(
        "s3", endpoint_url=url, aws_access_key_id="k", aws_secret_access_key="s", region_name="us-east-1"
    )
    client.create_bucket(Bucket=BUCKET)
    client.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
    if cors_origin:
        client.put_bucket_cors(
            Bucket=BUCKET,
            CORSConfiguration={
                "CORSRules": [
                    {
                        "AllowedOrigins": [cors_origin],
                        "AllowedMethods": ["PUT", "GET"],
                        "AllowedHeaders": ["*"],
                    }
                ]
            },
        )
    try:
        yield url
    finally:
        server.stop()


def s3_settings(endpoint: str, **extra: object):
    from datacourt.config import Settings

    return Settings(
        storage_backend="s3",
        S3_ENDPOINT_URL=endpoint,
        S3_BUCKET=BUCKET,
        S3_ACCESS_KEY_ID="k",
        S3_SECRET_ACCESS_KEY="s",
        S3_REGION="us-east-1",
        **extra,
    )
