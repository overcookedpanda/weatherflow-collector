"""AWS Signature Version 4 request signing, standard library only.

Used to sign Prometheus remote write requests to Amazon Managed Service for
Prometheus (service name "aps"). Follows
https://docs.aws.amazon.com/IAM/latest/UserGuide/create-signed-request.html
"""

import hashlib
import hmac
from datetime import datetime, timezone
from urllib.parse import parse_qsl, quote, urlsplit

ALGORITHM = "AWS4-HMAC-SHA256"


def _hmac(key, message):
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _uri_encode(value, safe="-_.~"):
    return quote(value, safe=safe)


def _canonical_uri(path):
    # Every service but S3 encodes each path segment twice.
    segments = (path or "/").split("/")
    return "/".join(_uri_encode(_uri_encode(segment)) for segment in segments) or "/"


def _canonical_query(query):
    pairs = parse_qsl(query, keep_blank_values=True)
    encoded = sorted((_uri_encode(k), _uri_encode(v)) for k, v in pairs)
    return "&".join(f"{k}={v}" for k, v in encoded)


def _trim(value):
    return " ".join(str(value).strip().split())


def signing_key(secret_key, date_stamp, region, service):
    key = _hmac(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    key = _hmac(key, region)
    key = _hmac(key, service)
    return _hmac(key, "aws4_request")


def sign(
    method,
    url,
    headers,
    body,
    *,
    region,
    service,
    access_key,
    secret_key,
    session_token=None,
    now=None,
):
    """Return the headers to add to the request so it is signed.

    headers are the request's own headers, all of which are signed. The
    returned dict holds Host, X-Amz-Date, Authorization and, with temporary
    credentials, X-Amz-Security-Token.
    """
    if isinstance(body, str):
        body = body.encode("utf-8")
    now = now or datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    parts = urlsplit(url)
    added = {"Host": parts.netloc, "X-Amz-Date": amz_date}
    if session_token:
        added["X-Amz-Security-Token"] = session_token

    all_headers = {k.lower(): _trim(v) for k, v in {**headers, **added}.items()}
    signed_names = sorted(all_headers)
    canonical_headers = "".join(f"{name}:{all_headers[name]}\n" for name in signed_names)
    signed_headers = ";".join(signed_names)
    payload_hash = hashlib.sha256(body or b"").hexdigest()

    canonical_request = "\n".join(
        [
            method.upper(),
            _canonical_uri(parts.path),
            _canonical_query(parts.query),
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            ALGORITHM,
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signature = hmac.new(
        signing_key(secret_key, date_stamp, region, service),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    added["Authorization"] = (
        f"{ALGORITHM} Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return added
