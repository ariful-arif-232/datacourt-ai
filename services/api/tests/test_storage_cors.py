"""`datacourt-storage-cors`: the bucket CORS rule browsers need for presigned uploads."""

from __future__ import annotations

import base64
import json
from dataclasses import asdict

import httpx
import pytest
from botocore.exceptions import ClientError

from datacourt import storage_cors as cors
from datacourt.config import Settings
from datacourt.storage import S3ObjectStore
from tests.s3_fixture import BUCKET, moto_s3

ORIGIN = "https://datacourt-ai.vercel.app"
KEY_ID = "004a1b2c3d4e5f60000000001"
SECRET = "K004FakeApplicationKeyNeverPrinted0123"  # moto accepts any credentials
CANNED = {  # what a web-console "share with this one origin" rule looks like: downloads only
    "ID": "downloadFromThisOrigin",
    "AllowedOrigins": [ORIGIN],
    "AllowedMethods": ["GET", "HEAD"],
    "AllowedHeaders": ["range"],
    "MaxAgeSeconds": 3600,
}
TOKEN = "native-api-authorization-token-never-printed"
NATIVE_API = "https://api000.backblazeb2.example"
CANNED_NATIVE = {  # the same preset as the B2 Native API lists it
    "corsRuleName": "downloadFromThisOrigin",
    "allowedOrigins": [ORIGIN],
    "allowedOperations": ["b2_download_file_by_id", "b2_download_file_by_name", "s3_head", "s3_get"],
    "allowedHeaders": ["authorization", "range"],
    "exposeHeaders": None,
    "maxAgeSeconds": 3600,
}
PARTNER_NATIVE = {  # native-only operations: invisible through the S3 API, kept as it is
    "corsRuleName": "partner-downloads",
    "allowedOrigins": ["https://partner.example.com"],
    "allowedOperations": ["b2_download_file_by_name"],
    "allowedHeaders": [],
    "exposeHeaders": None,
    "maxAgeSeconds": 60,
}
WEB_CONSOLE_KEY = [  # a key for one bucket with "Read and Write" access
    "deleteFiles",
    "listBuckets",
    "listFiles",
    "readBucketEncryption",
    "readBuckets",
    "readFiles",
    "shareFiles",
    "writeFiles",
]
STAGING = {
    "ID": "staging-downloads",
    "AllowedOrigins": ["https://staging.example.com", ORIGIN],
    "AllowedMethods": ["GET"],
    "MaxAgeSeconds": 600,
}


class FakeB2:
    """Answers CORS preflights the way Backblaze B2 documents it, from the bucket's real rules: the
    first rule whose origins, methods and headers all match answers; otherwise HTTP 403. moto's own
    server accepts every preflight, so it cannot stand in for this part. `stale` refuses that many
    preflights first, like servers that have not picked up a changed rule yet."""

    def __init__(self, store: S3ObjectStore, stale: int = 0) -> None:
        self.store = store
        self.stale = stale
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.method == "OPTIONS"
        if self.stale > 0:
            self.stale -= 1
            return httpx.Response(403)
        origin = request.headers["origin"]
        method = request.headers["access-control-request-method"]
        wanted = {
            h.strip().lower()
            for h in request.headers.get("access-control-request-headers", "").split(",")
            if h.strip()
        }
        for rule in cors.read_rules(self.store):
            allowed = {h.lower() for h in rule.get("AllowedHeaders", [])}
            if (
                (origin in rule["AllowedOrigins"] or "*" in rule["AllowedOrigins"])
                and method in rule["AllowedMethods"]
                and ("*" in allowed or wanted <= allowed)
            ):
                headers = {
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Methods": ", ".join(rule["AllowedMethods"]),
                }
                if wanted:
                    headers["Access-Control-Allow-Headers"] = ", ".join(sorted(wanted))
                return httpx.Response(200, headers=headers)
        return httpx.Response(403)


class FakeB2Native:
    """The B2 Native API calls of the fallback, over the moto bucket. Like B2, S3 PutBucketCors is
    refused while rules set through this API exist; their S3 operations are mirrored into the
    bucket's S3 rules, which is what the preflight fake and GetBucketCors see."""

    def __init__(self, store: S3ObjectStore, monkeypatch, rules: list[dict], capabilities: list[str]):
        self.store = store
        self.rules = [dict(r) for r in rules]
        self.capabilities = capabilities
        self.revision = 7
        self.updates: list[dict] = []
        self._put_s3 = store.client.put_bucket_cors
        self._mirror()

        def put_bucket_cors(**kw):
            if self.rules:
                raise ClientError(
                    {
                        "Error": {
                            "Code": "InvalidRequest",
                            "Message": "The bucket contains B2 Native CORS rules. Please use B2 Native "
                            "API instead. (See https://www.backblaze.com/docs/cloud-storage-cors)",
                        },
                        "ResponseMetadata": {"HTTPStatusCode": 400},
                    },
                    "PutBucketCors",
                )
            return self._put_s3(**kw)

        monkeypatch.setattr(store.client, "put_bucket_cors", put_bucket_cors)

    def _mirror(self) -> None:
        methods = {"s3_get": "GET", "s3_head": "HEAD", "s3_put": "PUT"}
        s3_rules = []
        for r in self.rules:
            s3_methods = [methods[o] for o in r["allowedOperations"] if o in methods]
            if s3_methods:
                s3_rules.append(
                    {
                        "AllowedOrigins": r["allowedOrigins"],
                        "AllowedMethods": s3_methods,
                        "AllowedHeaders": r.get("allowedHeaders") or [],
                        "ExposeHeaders": r.get("exposeHeaders") or [],
                        "MaxAgeSeconds": r["maxAgeSeconds"],
                        **({"ID": r["corsRuleName"]} if r["corsRuleName"] == cors.RULE_ID else {}),
                    }
                )
        if s3_rules:
            self._put_s3(Bucket=BUCKET, CORSConfiguration={"CORSRules": s3_rules})
        else:
            self.store.client.delete_bucket_cors(Bucket=BUCKET)

    def _bucket(self) -> dict:
        return {
            "accountId": "acct-1",
            "bucketId": "bucket-1",
            "bucketName": BUCKET,
            "bucketType": "allPrivate",
            "corsRules": self.rules,
            "revision": self.revision,
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/b2api/v2/b2_authorize_account":
            assert request.url.host == "api.backblazeb2.com"
            basic = base64.b64encode(f"{KEY_ID}:{SECRET}".encode()).decode()
            assert request.headers["authorization"] == f"Basic {basic}"
            allowed = {"bucketId": "bucket-1", "bucketName": BUCKET, "capabilities": self.capabilities}
            return httpx.Response(
                200,
                json={
                    "accountId": "acct-1",
                    "apiUrl": NATIVE_API,
                    "authorizationToken": TOKEN,
                    "allowed": allowed,
                },
            )
        assert str(request.url).startswith(NATIVE_API) and request.headers["authorization"] == TOKEN
        body = json.loads(request.content)
        if request.url.path == "/b2api/v2/b2_list_buckets":
            assert body == {"accountId": "acct-1", "bucketName": BUCKET}
            return httpx.Response(200, json={"buckets": [self._bucket()]})
        assert request.url.path == "/b2api/v2/b2_update_bucket"
        # Only the bucket's id and CORS rules: its type and other settings are never sent. Like B2's
        # v2 API, a field it does not know (ifRevisionMatches, say) fails the whole request.
        self.updates.append(body)
        unknown = set(body) - {"accountId", "bucketId", "corsRules", "bucketType", "bucketInfo"}
        if unknown:
            message = f"unknown field in UpdateBucketRequestFromB2: {', '.join(sorted(unknown))}"
            return httpx.Response(400, json={"status": 400, "code": "bad_request", "message": message})
        assert set(body) == {"accountId", "bucketId", "corsRules"} and body["bucketId"] == "bucket-1"
        if "writeBuckets" not in self.capabilities:
            return httpx.Response(
                401, json={"status": 401, "code": "unauthorized", "message": "not entitled"}
            )
        self.rules = body["corsRules"]
        self.revision += 1
        self._mirror()
        return httpx.Response(200, json=self._bucket())


def _b2_http(store: S3ObjectStore, native: FakeB2Native) -> httpx.Client:
    preflights = FakeB2(store)
    return _http(lambda r: preflights(r) if r.method == "OPTIONS" else native(r))


@pytest.fixture(scope="module")
def endpoint():
    with moto_s3() as url:
        yield url


@pytest.fixture()
def store(endpoint) -> S3ObjectStore:
    s = S3ObjectStore(
        Settings(
            storage_backend="s3",
            S3_ENDPOINT_URL=endpoint,
            S3_BUCKET=BUCKET,
            S3_ACCESS_KEY_ID=KEY_ID,
            S3_SECRET_ACCESS_KEY=SECRET,
            S3_REGION="us-east-1",
        )
    )
    s.client.delete_bucket_cors(Bucket=BUCKET)
    return s


def _http(fake: FakeB2 | object) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(fake))  # type: ignore[arg-type]


def _outcome(report: cors.CorsReport, step: str) -> str:
    return {s.name: s.outcome for s in report.steps}[step]


def _assert_public_safe(text: str) -> None:
    for secret in (KEY_ID, SECRET, TOKEN, NATIVE_API, "X-Amz-", "Signature", cors.PROBE_KEY, BUCKET):
        assert secret not in text


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (ORIGIN, ORIGIN),
        ("  HTTPS://DataCourt-AI.Vercel.app ", ORIGIN),
        ("https://app.example.com:8443", "https://app.example.com:8443"),
        ("http://localhost:3000", "http://localhost:3000"),
    ],
)
def test_origins_are_exact(value, expected):
    assert cors.normalize_origin(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        f"{ORIGIN}/",
        f"{ORIGIN}/app",
        f"{ORIGIN}?x=1",
        "https://*.vercel.app",
        "*",
        "",
        "http://datacourt-ai.vercel.app",
        "https://user@datacourt-ai.vercel.app",
        "https://vercel",
        "https://-bad.vercel.app",
        "https://datacourt-ai.vercel.app\nX-Injected: 1",
    ],
)
def test_other_origins_are_refused(value):
    with pytest.raises(ValueError):
        cors.normalize_origin(value)


def test_rule_and_plan():
    rule = cors.desired_rule([ORIGIN, ORIGIN.upper()])
    assert rule == {
        "ID": "datacourt-browser-uploads",
        "AllowedOrigins": [ORIGIN],
        "AllowedMethods": ["GET", "PUT", "HEAD"],
        "AllowedHeaders": ["content-type"],
        "ExposeHeaders": ["ETag"],
        "MaxAgeSeconds": 3600,
    }
    stale_ours = {**rule, "AllowedOrigins": ["https://old.example.com"]}
    unnamed = {"AllowedOrigins": ["*"], "AllowedMethods": ["GET"]}
    # Ours first and the only rule for the app origin; other origins keep their rules.
    plan = cors.plan_rules([CANNED, stale_ours, STAGING, unnamed], rule)
    assert plan == [
        rule,
        {**STAGING, "AllowedOrigins": ["https://staging.example.com"]},
        {**unnamed, "ID": "kept-rule-2"},
    ]
    assert cors.plan_rules([CANNED, STAGING], rule, replace_all=True) == [rule]
    with pytest.raises(ValueError):
        cors.desired_rule([ORIGIN], max_age=90_000)
    with pytest.raises(ValueError):
        cors.desired_rule([])


def test_sets_the_rule_that_browser_uploads_need(store, capsys):
    store.client.put_bucket_cors(Bucket=BUCKET, CORSConfiguration={"CORSRules": [CANNED, STAGING]})
    fake = FakeB2(store)
    http = _http(fake)
    urls = cors.probe_urls(store)
    # The situation the storage check reported: downloads allowed, the upload preflight refused.
    before = cors.run_probes(http, urls, ORIGIN, cors.UPLOAD_PROBES)
    assert [pf.status for _, pf in before] == [403, 403]

    sent: list[dict] = []
    store.client.meta.events.register(
        "before-send.s3.PutBucketCors", lambda request, **_: sent.append(dict(request.headers))
    )
    report = cors.configure([ORIGIN], store=store, http=http, wait_seconds=0)
    assert report.ok and report.changed, report.steps
    assert cors.read_rules(store) == [
        cors.desired_rule([ORIGIN]),
        {**STAGING, "AllowedOrigins": ["https://staging.example.com"]},
    ]
    assert len(sent) == 1 and "Content-MD5" in sent[0]  # accepted by every S3-compatible service
    outcomes = {s.name: s.outcome for s in report.steps}
    assert all(o == "pass" for o in outcomes.values()), report.steps
    assert f"preflight from {ORIGIN}: single-file upload (PUT with Content-Type)" in outcomes
    assert f"preflight from {ORIGIN}: multipart part upload (PUT)" in outcomes
    assert outcomes["upload preflight from another origin is refused"] == "pass"
    for _, pf in cors.run_probes(http, urls, ORIGIN, cors.UPLOAD_PROBES + cors.DOWNLOAD_PROBES):
        assert pf.status == 200

    # Running it again changes nothing.
    again = cors.configure([ORIGIN], store=store, http=http, wait_seconds=0)
    assert again.ok and not again.changed and again.unchanged and len(sent) == 1
    assert any(s.detail == "already up to date: not rewritten" for s in again.steps)

    cors._print(report)
    out = capsys.readouterr().out
    assert "Browser uploads are allowed." in out and ORIGIN in out
    _assert_public_safe(out + json.dumps(asdict(report)))
    # Preflights never carry credentials.
    assert all("authorization" not in r.headers for r in fake.requests)


def test_dry_run_changes_nothing(store):
    store.client.put_bucket_cors(Bucket=BUCKET, CORSConfiguration={"CORSRules": [CANNED]})
    report = cors.configure([ORIGIN], store=store, http=_http(FakeB2(store)), dry_run=True)
    assert report.ok and not report.changed and report.after is None
    assert cors.read_rules(store) == [CANNED]
    assert [r["ID"] for r in report.planned] == ["datacourt-browser-uploads"]
    upload = [s for s in report.steps if "upload (PUT" in s.name]
    assert upload and all(s.outcome == "info" and "HTTP 403" in s.detail for s in upload)


def test_waits_for_the_new_rule_to_take_effect(store, monkeypatch):
    monkeypatch.setattr(cors.time, "sleep", lambda _s: None)
    report = cors.configure([ORIGIN], store=store, http=_http(FakeB2(store, stale=5)), wait_seconds=60)
    assert report.ok, report.steps
    report = cors.configure([ORIGIN], store=store, http=_http(FakeB2(store, stale=100)), wait_seconds=0)
    assert not report.ok
    assert any(s.outcome == "fail" and "HTTP 403" in s.detail for s in report.steps)


def test_permission_errors_are_explained_without_secrets(store, monkeypatch, capsys):
    store.client.put_bucket_cors(Bucket=BUCKET, CORSConfiguration={"CORSRules": [CANNED]})

    def denied(**_kw):
        raise ClientError(
            {
                "Error": {"Code": "AccessDenied", "Message": f"not entitled: key {KEY_ID}"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            "PutBucketCors",
        )

    monkeypatch.setattr(store.client, "put_bucket_cors", denied)
    fake = FakeB2(store)
    report = cors.configure([ORIGIN], store=store, http=_http(fake))
    assert not report.ok and not report.changed
    failure = report.steps[-1]
    assert failure.outcome == "fail" and "writeBuckets" in failure.detail
    assert "Nothing was changed" in failure.detail and "HTTP 403" in failure.detail
    _assert_public_safe(failure.detail)
    assert cors.read_rules(store) == [CANNED] and fake.requests == []
    cors._print(report)
    out = capsys.readouterr().out
    assert "Rules NOT written (1):" in out and "Bucket CORS is NOT configured correctly." in out
    _assert_public_safe(out)


def test_network_errors_never_print_the_signed_url(store):
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    store.client.put_bucket_cors(
        Bucket=BUCKET, CORSConfiguration={"CORSRules": [cors.desired_rule([ORIGIN])]}
    )
    report = cors.configure([ORIGIN], store=store, http=_http(unreachable), wait_seconds=0)
    assert not report.ok
    details = " ".join(s.detail for s in report.steps)
    assert "no response (ConnectError)" in details
    _assert_public_safe(details)


def test_rules_that_let_any_origin_upload_are_flagged(store):
    anyone = {
        "ID": "anyone-uploads",
        "AllowedOrigins": ["*"],
        "AllowedMethods": ["PUT"],
        "AllowedHeaders": ["*"],
    }
    store.client.put_bucket_cors(Bucket=BUCKET, CORSConfiguration={"CORSRules": [anyone]})
    report = cors.configure([ORIGIN], store=store, http=_http(FakeB2(store)), wait_seconds=0)
    assert report.ok
    assert _outcome(report, "upload preflight from another origin is refused") == "warn"
    report = cors.configure(
        [ORIGIN], store=store, http=_http(FakeB2(store)), wait_seconds=0, replace_all=True
    )
    assert report.ok and [r["ID"] for r in cors.read_rules(store)] == ["datacourt-browser-uploads"]
    assert _outcome(report, "upload preflight from another origin is refused") == "pass"


def test_over_http_against_a_service_that_merges_matching_rules(store):
    """moto's server combines every rule matching an origin (the last one's methods win), unlike B2.
    With the download-only rule left in place, a browser would block the upload; the app's origin
    therefore gets exactly one rule."""
    store.client.put_bucket_cors(
        Bucket=BUCKET,
        CORSConfiguration={"CORSRules": [cors.desired_rule([ORIGIN]), CANNED]},
    )
    with httpx.Client() as http:
        shadowed = cors.run_probes(http, cors.probe_urls(store), ORIGIN, cors.UPLOAD_PROBES)
        assert [pf.problem(ORIGIN, p) for p, pf in shadowed] == [
            "HTTP 200 but Access-Control-Allow-Methods lacks PUT"
        ] * 2
        report = cors.configure([ORIGIN], store=store, http=http, wait_seconds=0)
    assert report.ok and report.changed, report.steps
    assert _outcome(report, "upload preflight from another origin is refused") == "pass"


def test_preflight_problems_are_named():
    put_ct = cors.UPLOAD_PROBES[0]
    ok = cors.Preflight(200, ORIGIN, frozenset({"PUT"}), frozenset({"content-type"}))
    assert ok.allows(ORIGIN, put_ct) and ok.problem(ORIGIN, put_ct) == ""
    assert cors.Preflight(403).problem(ORIGIN, put_ct) == "HTTP 403"
    assert "Allow-Origin" in cors.Preflight(200, "https://other.example").problem(ORIGIN, put_ct)
    no_header = cors.Preflight(200, ORIGIN, frozenset({"PUT"}))
    assert no_header.problem(ORIGIN, put_ct) == "HTTP 200 but Access-Control-Allow-Headers lacks content-type"
    assert no_header.allows(ORIGIN, cors.UPLOAD_PROBES[1])  # part uploads send no extra header
    assert cors.Preflight(200, "*", frozenset(), frozenset({"*"})).allows(ORIGIN, cors.DOWNLOAD_PROBES[0])


def test_native_rule_form():
    rule = cors.native_rule(cors.desired_rule([ORIGIN]))
    assert rule == {
        "corsRuleName": "datacourt-browser-uploads",
        "allowedOrigins": [ORIGIN],
        "allowedOperations": ["s3_get", "s3_put", "s3_head"],
        "allowedHeaders": ["content-type"],
        "exposeHeaders": ["ETag"],
        "maxAgeSeconds": 3600,
    }
    shared = {**PARTNER_NATIVE, "allowedOrigins": ["https://partner.example.com", ORIGIN]}
    assert cors.plan_native_rules([CANNED_NATIVE, shared], rule) == [rule, PARTNER_NATIVE]
    assert cors.plan_native_rules([CANNED_NATIVE, shared], rule, replace_all=True) == [rule]


def test_rules_set_through_the_native_api_are_replaced_through_it(store, monkeypatch, capsys):
    """What the production bucket had: the web console's download preset, a B2 Native API rule,
    so B2 refused PutBucketCors ("The bucket contains B2 Native CORS rules")."""
    native = FakeB2Native(
        store, monkeypatch, [CANNED_NATIVE, PARTNER_NATIVE], [*WEB_CONSOLE_KEY, "writeBuckets"]
    )
    http = _b2_http(store, native)
    before = cors.run_probes(http, cors.probe_urls(store), ORIGIN, cors.UPLOAD_PROBES)
    assert [pf.status for _, pf in before] == [403, 403]

    report = cors.configure([ORIGIN], store=store, http=http, wait_seconds=0)
    assert report.ok and report.changed, report.steps
    outcomes = {s.name: s.outcome for s in report.steps}
    assert outcomes["write rules (PutBucketCors)"] == "info"
    assert outcomes["write rules (B2 Native API b2_update_bucket, CORS rules only)"] == "pass"
    assert outcomes["bucket is still private"] == "pass"
    assert all(o == "pass" for n, o in outcomes.items() if n.startswith("preflight")), report.steps
    assert outcomes["upload preflight from another origin is refused"] == "pass"
    # Ours first; the preset for the same origin is replaced; the other origin's rule is untouched.
    assert native.rules == [cors.native_rule(cors.desired_rule([ORIGIN])), PARTNER_NATIVE]
    assert len(native.updates) == 1

    cors._print(report)
    out = capsys.readouterr().out
    assert "Rules written through the B2 Native API (2):" in out and "Browser uploads are allowed." in out
    _assert_public_safe(out + json.dumps(asdict(report)))

    again = cors.configure([ORIGIN], store=store, http=http, wait_seconds=0)
    assert again.ok and again.unchanged and len(native.updates) == 1


def test_the_native_fallback_needs_the_write_buckets_capability(store, monkeypatch, capsys):
    native = FakeB2Native(store, monkeypatch, [CANNED_NATIVE], WEB_CONSOLE_KEY)
    report = cors.configure([ORIGIN], store=store, http=_b2_http(store, native), wait_seconds=0)
    assert not report.ok and not report.changed
    failure = report.steps[-1]
    assert failure.outcome == "fail" and "lacks the writeBuckets capability" in failure.detail
    assert "readBuckets" in failure.detail and "Nothing was changed" in failure.detail
    assert native.updates == [] and native.rules == [CANNED_NATIVE]
    cors._print(report)
    _assert_public_safe(capsys.readouterr().out)


def test_cli_rejects_a_bad_origin_before_any_request(capsys):
    assert cors.main(["--origin", f"{ORIGIN}/"]) == 2
    assert "invalid origin" in capsys.readouterr().err
