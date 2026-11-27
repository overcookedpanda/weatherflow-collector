"""Tests for SigV4 signing against AWS's published example."""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from utils import sigv4  # noqa: E402

# The example from AWS's Signature Version 4 documentation (IAM ListUsers).
EXAMPLE_KEY = "AKIDEXAMPLE"
EXAMPLE_SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
EXAMPLE_TIME = datetime(2015, 8, 30, 12, 36, 0, tzinfo=timezone.utc)


def test_matches_aws_documented_signature():
    headers = sigv4.sign(
        "GET",
        "https://iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08",
        {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        b"",
        region="us-east-1",
        service="iam",
        access_key=EXAMPLE_KEY,
        secret_key=EXAMPLE_SECRET,
        now=EXAMPLE_TIME,
    )
    assert headers["X-Amz-Date"] == "20150830T123600Z"
    assert headers["Host"] == "iam.amazonaws.com"
    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/iam/aws4_request, "
        "SignedHeaders=content-type;host;x-amz-date, "
        "Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7"
    )


def test_session_token_is_signed():
    headers = sigv4.sign(
        "POST",
        "https://aps-workspaces.us-east-1.amazonaws.com/workspaces/ws-example/api/v1/remote_write",
        {"Content-Type": "application/x-protobuf"},
        b"payload",
        region="us-east-1",
        service="aps",
        access_key=EXAMPLE_KEY,
        secret_key=EXAMPLE_SECRET,
        session_token="example-session-token",
        now=EXAMPLE_TIME,
    )
    assert headers["X-Amz-Security-Token"] == "example-session-token"
    assert "x-amz-security-token" in headers["Authorization"]
    assert "/us-east-1/aps/aws4_request" in headers["Authorization"]


def test_signature_depends_on_body():
    def signature(body):
        return sigv4.sign(
            "POST",
            "https://example.com/api/v1/remote_write",
            {},
            body,
            region="us-east-1",
            service="aps",
            access_key=EXAMPLE_KEY,
            secret_key=EXAMPLE_SECRET,
            now=EXAMPLE_TIME,
        )["Authorization"]

    assert signature(b"a") != signature(b"b")
