"""AWS Signature Version 4 (agents/common/sigv4.py) against AWS's published example: the IAM ListUsers request
signed with the documentation's example keys on 30 August 2015."""
import datetime

from agents.common import sigv4

SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"


def test_the_signing_key_matches_aws_example():
    assert sigv4.signing_key(SECRET, "20150830", "us-east-1", "iam").hex() == \
        "c4afb1cc5771d871763a393e44b703571b55cc28424d1a5e86da6ed3c154a4b9"


def test_the_signature_matches_aws_example():
    h = sigv4.sign("GET", "https://iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08",
                   {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}, b"",
                   access_key="AKIDEXAMPLE", secret_key=SECRET, region="us-east-1", service="iam",
                   now=datetime.datetime(2015, 8, 30, 12, 36, 0, tzinfo=datetime.timezone.utc))
    assert h["X-Amz-Date"] == "20150830T123600Z"
    assert h["Authorization"] == ("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/iam/aws4_request, "
                                  "SignedHeaders=content-type;host;x-amz-date, "
                                  "Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7")


def test_temporary_credentials_add_and_sign_the_token():
    h = sigv4.sign("POST", "https://bedrock-runtime.us-east-2.amazonaws.com/model/m%3A0/invoke", {"content-type": "application/json"},
                   b"{}", access_key="A", secret_key="S", region="us-east-2", service="bedrock", session_token="T",
                   now=datetime.datetime(2026, 10, 16, tzinfo=datetime.timezone.utc))
    assert h["X-Amz-Security-Token"] == "T" and "x-amz-security-token" in h["Authorization"]
