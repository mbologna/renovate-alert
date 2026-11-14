import base64
import datetime as dt
import gzip
import hashlib
import io
import json
import random
import urllib.parse

import pytest
from test_alert import ABORTED_LOG

import renovate_alert as ra
from renovate_alert import gcs


# A throwaway RSA key made here, so no key file sits in the repository.
def _prime(bits, rng):
    while True:
        n = rng.getrandbits(bits) | (1 << (bits - 1)) | 1
        if all(pow(a, n - 1, n) == 1 for a in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37)) and _miller_rabin(n, rng):
            return n


def _miller_rabin(n, rng, rounds=20):
    d, s = n - 1, 0
    while d % 2 == 0:
        d, s = d // 2, s + 1
    for _ in range(rounds):
        x = pow(rng.randrange(2, n - 1), d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _tlv(tag, value):
    n = len(value)
    length = (
        bytes([n])
        if n < 0x80
        else bytes([0x80 | ((n.bit_length() + 7) // 8)]) + n.to_bytes((n.bit_length() + 7) // 8, "big")
    )
    return bytes([tag]) + length + value


def _int(i):
    return _tlv(0x02, i.to_bytes(i.bit_length() // 8 + 1, "big"))


@pytest.fixture(scope="module")
def key():
    rng = random.Random(42)
    e = 65537
    while True:
        p, q = _prime(512, rng), _prime(512, rng)
        phi = (p - 1) * (q - 1)
        if p != q and phi % e:
            break
    n, d = p * q, pow(e, -1, phi)
    rsa = _tlv(0x30, b"".join(_int(i) for i in (0, n, e, d, p, q, d % (p - 1), d % (q - 1), pow(q, -1, p))))
    algorithm = _tlv(0x30, _tlv(0x06, bytes.fromhex("2a864886f70d010101")) + b"\x05\x00")
    der = _tlv(0x30, _int(0) + algorithm + _tlv(0x04, rsa))
    pem = "-----BEGIN PRIVATE KEY-----\n" + base64.encodebytes(der).decode() + "-----END PRIVATE KEY-----\n"
    return {"pem": pem, "n": n, "e": e, "d": d}


def verify(key, message, signature):
    """The PKCS#1 v1.5 encoding recovered with the public key."""
    k = (key["n"].bit_length() + 7) // 8
    encoded = pow(int.from_bytes(signature, "big"), key["e"], key["n"]).to_bytes(k, "big")
    digest = gcs.SHA256_PREFIX + hashlib.sha256(message).digest()
    return encoded == b"\x00\x01" + b"\xff" * (k - len(digest) - 3) + b"\x00" + digest


@pytest.mark.parametrize(
    "url, parsed",
    [
        ("gs://logs", ("logs", "")),
        ("gs://logs/", ("logs", "")),
        ("gs://logs/renovate", ("logs", "renovate/")),
        ("gs://logs/a/b/", ("logs", "a/b/")),
    ],
)
def test_parse_url(url, parsed):
    assert gcs.parse_url(url) == parsed


@pytest.mark.parametrize("url", ["s3://logs", "gs://", "/logs"])
def test_parse_url_rejects(url):
    with pytest.raises(ValueError):
        gcs.parse_url(url)


def test_private_key_and_sign(key):
    assert gcs.private_key(key["pem"]) == (key["n"], key["d"])
    signature = gcs.sign(key["pem"], b"header.claims")
    assert len(signature) == 128
    assert verify(key, b"header.claims", signature)
    assert not verify(key, b"header.other", signature)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@pytest.fixture
def credentials(key, tmp_path, monkeypatch):
    path = tmp_path / "key.json"
    path.write_text(
        json.dumps(
            {
                "type": "service_account",
                "client_email": "reader@project.iam.gserviceaccount.com",
                "private_key_id": "abc123",
                "private_key": key["pem"],
                "token_uri": "https://oauth2.example/token",
            }
        )
    )
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(path))
    monkeypatch.setitem(gcs._token, "value", "")
    monkeypatch.setitem(gcs._token, "expires", 0.0)
    return path


def test_token_signs_a_jwt_and_is_reused(key, credentials, monkeypatch):
    calls = []

    def urlopen(req, timeout):
        calls.append(req)
        return FakeResponse(json.dumps({"access_token": "tok", "expires_in": 3600}).encode())

    monkeypatch.setattr(gcs.urllib.request, "urlopen", urlopen)
    assert gcs.token(now=1000) == "tok"
    assert gcs.token(now=2000) == "tok"
    assert len(calls) == 1
    req = calls[0]
    assert req.full_url == "https://oauth2.example/token"
    form = urllib.parse.parse_qs(req.data.decode())
    assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
    header, claims, signature = form["assertion"][0].split(".")
    pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))  # noqa: E731
    assert json.loads(pad(header)) == {"alg": "RS256", "typ": "JWT", "kid": "abc123"}
    assert json.loads(pad(claims)) == {
        "iss": "reader@project.iam.gserviceaccount.com",
        "scope": gcs.SCOPE,
        "aud": "https://oauth2.example/token",
        "iat": 1000,
        "exp": 4600,
    }
    assert verify(key, f"{header}.{claims}".encode(), pad(signature))
    # Renewed shortly before it expires.
    gcs.token(now=4560)
    assert len(calls) == 2


def test_token_needs_credentials(monkeypatch):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setitem(gcs._token, "value", "")
    with pytest.raises(RuntimeError, match="GOOGLE_APPLICATION_CREDENTIALS"):
        gcs.token()


def test_list_objects_follows_pages_and_open_object(monkeypatch):
    monkeypatch.setattr(gcs, "token", lambda: "tok")
    pages = {
        "": {"items": [{"name": "renovate-1.log", "updated": "2026-11-14T00:30:00.000Z"}], "nextPageToken": "p2"},
        "p2": {"items": [{"name": "renovate-2.log.gz", "updated": "2026-11-14T01:00:00Z"}]},
    }
    urls = []

    def urlopen(req, timeout):
        urls.append(req.full_url)
        assert req.headers["Authorization"] == "Bearer tok"
        url = urllib.parse.urlsplit(req.full_url)
        if url.query == "alt=media":
            return FakeResponse(b"content")
        query = urllib.parse.parse_qs(url.query)
        assert query["prefix"] == ["logs/renovate-"] and query["delimiter"] == ["/"]
        return FakeResponse(json.dumps(pages[query.get("pageToken", [""])[0]]).encode())

    monkeypatch.setattr(gcs.urllib.request, "urlopen", urlopen)
    assert gcs.list_objects("bucket", "logs/renovate-") == [
        ("renovate-1.log", dt.datetime(2026, 11, 14, 0, 30, tzinfo=dt.UTC).timestamp()),
        ("renovate-2.log.gz", dt.datetime(2026, 11, 14, 1, 0, tzinfo=dt.UTC).timestamp()),
    ]
    assert len(urls) == 2
    with gcs.open_object("bucket", "logs/renovate-1.log") as f:
        assert f.read() == b"content"
    assert urls[-1] == "https://storage.googleapis.com/storage/v1/b/bucket/o/logs%2Frenovate-1.log?alt=media"


def test_run_logs_from_gcs(tmp_path, monkeypatch):
    """The checks read the bucket's logs as they read LOG_DIR's, and keep their state in STATE_DIR."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(ra, "GCS_BUCKET", "bucket")
    monkeypatch.setattr(ra, "GCS_PREFIX", "")
    monkeypatch.setattr(ra, "LOG_SOURCE", "gs://bucket")
    monkeypatch.setattr(ra, "STATE", str(state_dir / "alert-state.json"))
    monkeypatch.setattr(ra, "API", "")
    sent = []
    monkeypatch.setattr(ra, "notify", lambda url, title, body, type_: sent.append((title, body)) or True)
    now = ra.NOW.timestamp()
    objects = {
        "renovate-20261114-001500.log": (now - 7200, ABORTED_LOG.encode()),
        "renovate-20261114-004500.log.gz": (now - 3600, gzip.compress(ABORTED_LOG.encode())),
        "renovate-locks.json": (now, b"not a log"),
    }
    listed = []

    def list_objects(bucket, prefix):
        listed.append((bucket, prefix))
        return [(name, mtime) for name, (mtime, _) in objects.items()]

    monkeypatch.setattr(ra.gcs, "list_objects", list_objects)
    monkeypatch.setattr(ra.gcs, "open_object", lambda bucket, name: io.BytesIO(objects[name][1]))
    ra.main()
    assert listed == [("bucket", "renovate-")]
    assert len(sent) == 1
    assert "renovate-20261114-001500, renovate-20261114-004500 aborted" in sent[0][1]
    state = json.load(open(state_dir / "alert-state.json"))
    assert state["logs_read_until"] == now - 3600
    assert state["last_run_failed"] is True


def test_no_log_in_the_bucket_names_it(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "GCS_BUCKET", "bucket")
    monkeypatch.setattr(ra, "LOG_SOURCE", "gs://bucket/renovate")
    monkeypatch.setattr(ra, "STATE", str(tmp_path / "alert-state.json"))
    monkeypatch.setattr(ra, "API", "")
    sent = []
    monkeypatch.setattr(ra, "notify", lambda url, title, body, type_: sent.append((title, body)) or True)
    monkeypatch.setattr(ra.gcs, "list_objects", lambda bucket, prefix: [])
    ra.main()
    assert "No Renovate run log in gs://bucket/renovate." in sent[0][1]
