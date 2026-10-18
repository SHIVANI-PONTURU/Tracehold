"""AWS Signature Version 4 for one HTTPS request, with nothing but the standard library.

Used when the model is reached with AWS access keys instead of an API key: Claude Platform on AWS (service
"aws-external-anthropic") and Amazon Bedrock (service "bedrock"). Checked against AWS's published example
(tests/test_sigv4.py). Keys come from the standard variables AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY and, for
temporary credentials, AWS_SESSION_TOKEN.
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import os
from urllib.parse import quote, urlsplit


def _hmac(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret_key, date, region, service):
    k = _hmac(("AWS4" + secret_key).encode("utf-8"), date)
    k = _hmac(k, region)
    k = _hmac(k, service)
    return _hmac(k, "aws4_request")


def _canonical_query(query):
    if not query:
        return ""
    pairs = []
    for part in query.split("&"):
        k, _, v = part.partition("=")
        pairs.append((quote(k, safe="-_.~"), quote(v, safe="-_.~")))
    return "&".join(f"{k}={v}" for k, v in sorted(pairs))


def sign(method, url, headers, body, *, access_key, secret_key, region, service, session_token=None, now=None):
    """Headers to add to the request (Authorization, X-Amz-Date, and X-Amz-Security-Token when given).

    headers: the request's own headers that should be signed (content-type, ...); host is added here.
    body: the exact bytes that will be sent.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    parts = urlsplit(url)
    signed = {k.lower(): " ".join(str(v).strip().split()) for k, v in headers.items()}
    signed["host"] = parts.netloc
    signed["x-amz-date"] = amz_date
    if session_token:
        signed["x-amz-security-token"] = session_token
    names = sorted(signed)
    # every service except S3 encodes each path segment twice in the canonical request
    path = "/".join(quote(seg, safe="-_.~") for seg in (parts.path or "/").split("/")) or "/"
    payload = hashlib.sha256(body if isinstance(body, bytes) else body.encode("utf-8")).hexdigest()
    canonical = "\n".join([method.upper(), path, _canonical_query(parts.query),
                           "".join(f"{n}:{signed[n]}\n" for n in names), ";".join(names), payload])
    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    signature = hmac.new(signing_key(secret_key, date, region, service), to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    out = {"X-Amz-Date": amz_date,
           "Authorization": f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={';'.join(names)}, "
                            f"Signature={signature}"}
    if session_token:
        out["X-Amz-Security-Token"] = session_token
    return out


def env_credentials():
    """(access_key, secret_key, session_token) from the environment, or None."""
    a, s = os.environ.get("AWS_ACCESS_KEY_ID"), os.environ.get("AWS_SECRET_ACCESS_KEY")
    return (a, s, os.environ.get("AWS_SESSION_TOKEN") or None) if a and s else None
