"""Read run logs from a Google Cloud Storage bucket, with the standard library only.

LOG_SOURCE=gs://bucket[/prefix] lists and reads the run logs through the GCS
JSON API instead of LOG_DIR. Authentication is a service account key file
(GOOGLE_APPLICATION_CREDENTIALS), exchanged for an OAuth token with the
read-only storage scope. Signing that exchange takes RS256, which the standard
library lacks; PKCS#1 v1.5 signing is a single modular exponentiation, so it is
done here (sign) rather than pulling in a crypto package for one signature a
day.
"""

import base64
import datetime as dt
import hashlib
import json
import os
import time
import urllib.parse
import urllib.request

SCOPE = "https://www.googleapis.com/auth/devstorage.read_only"
API = "https://storage.googleapis.com/storage/v1"
TIMEOUT = 60
# DER DigestInfo prefix for SHA-256 (RFC 8017, section 9.2, note 1).
SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")

_token = {"value": "", "expires": 0.0}


def parse_url(url):
    """gs://bucket/some/prefix -> ("bucket", "some/prefix/"); the prefix is "" or ends in "/"."""
    if not url.startswith("gs://") or len(url) <= len("gs://"):
        raise ValueError(f"not a gs://bucket[/prefix] URL: {url}")
    bucket, _, prefix = url[len("gs://") :].partition("/")
    prefix = prefix.strip("/")
    return bucket, f"{prefix}/" if prefix else ""


def _der(data, i=0):
    """One DER element at data[i]: (tag, value bytes, index after it)."""
    tag, length = data[i], data[i + 1]
    i += 2
    if length & 0x80:
        n = length & 0x7F
        length = int.from_bytes(data[i : i + n], "big")
        i += n
    return tag, data[i : i + length], i + length


def _children(data):
    out, i = [], 0
    while i < len(data):
        tag, value, i = _der(data, i)
        out.append((tag, value))
    return out


def private_key(pem):
    """(n, d) of an unencrypted PKCS#8 RSA private key in PEM, as a service account key file holds it."""
    body = "".join(line for line in pem.strip().splitlines() if not line.startswith("-----"))
    _, info, _ = _der(base64.b64decode(body))  # PrivateKeyInfo
    version, algorithm, key = _children(info)[:3]
    if key[0] != 0x04:
        raise ValueError("not a PKCS#8 private key")
    _, rsa, _ = _der(key[1])  # RSAPrivateKey
    ints = [int.from_bytes(value, "big") for tag, value in _children(rsa)]
    return ints[1], ints[3]


def sign(pem, message):
    """RSASSA-PKCS1-v1_5 signature with SHA-256 (JWT's RS256) of message."""
    n, d = private_key(pem)
    k = (n.bit_length() + 7) // 8
    digest = SHA256_PREFIX + hashlib.sha256(message).digest()
    encoded = b"\x00\x01" + b"\xff" * (k - len(digest) - 3) + b"\x00" + digest
    return pow(int.from_bytes(encoded, "big"), d, n).to_bytes(k, "big")


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def token(now=None):
    """An access token for the key in GOOGLE_APPLICATION_CREDENTIALS, reused until shortly before it expires."""
    now = time.time() if now is None else now
    if _token["value"] and now < _token["expires"] - 60:
        return _token["value"]
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if not path:
        raise RuntimeError("LOG_SOURCE=gs://... needs GOOGLE_APPLICATION_CREDENTIALS (a service account key file).")
    with open(path) as f:
        key = json.load(f)
    uri = key.get("token_uri", "https://oauth2.googleapis.com/token")
    header = {"alg": "RS256", "typ": "JWT", "kid": key.get("private_key_id", "")}
    claims = {"iss": key["client_email"], "scope": SCOPE, "aud": uri, "iat": int(now), "exp": int(now) + 3600}
    unsigned = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    assertion = f"{unsigned}.{_b64(sign(key['private_key'], unsigned.encode()))}"
    body = urllib.parse.urlencode(
        {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}
    ).encode()
    with urllib.request.urlopen(urllib.request.Request(uri, data=body), timeout=TIMEOUT) as r:
        got = json.load(r)
    _token.update(value=got["access_token"], expires=now + got.get("expires_in", 3600))
    return _token["value"]


def _open(url):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token()}"})
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def list_objects(bucket, prefix):
    """(name, modification time as a Unix timestamp) of every object directly under prefix."""
    found, page = [], ""
    while True:
        query = {"prefix": prefix, "delimiter": "/", "fields": "items(name,updated),nextPageToken"}
        if page:
            query["pageToken"] = page
        with _open(f"{API}/b/{urllib.parse.quote(bucket, safe='')}/o?{urllib.parse.urlencode(query)}") as r:
            got = json.load(r)
        found += [(o["name"], dt.datetime.fromisoformat(o["updated"]).timestamp()) for o in got.get("items", [])]
        page = got.get("nextPageToken", "")
        if not page:
            return found


def open_object(bucket, name):
    """A binary stream of the object's content."""
    return _open(f"{API}/b/{urllib.parse.quote(bucket, safe='')}/o/{urllib.parse.quote(name, safe='')}?alt=media")
