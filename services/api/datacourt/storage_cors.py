"""Set the bucket's CORS rule for browser uploads (`datacourt-storage-cors`).

Browsers upload dataset archives straight to the private bucket with presigned PUT requests, so the
bucket has to answer their CORS preflight for the app's origin. This writes that rule through the
S3-compatible API (PutBucketCors): the exact origin, GET/PUT/HEAD, the Content-Type request header
and ETag exposed to the page. That rule alone answers the app's origin; rules for other origins
are kept (unless `--replace-all`). It then reads the rules back and sends the preflight requests a
browser sends (single-file upload with Content-Type, multipart part upload, download) to confirm
the bucket accepts them, and that another origin is refused.

Backblaze B2 refuses PutBucketCors while the bucket has rules set through its Native API (the web
console's CORS presets are such rules). The same rule, with the same S3 operations, is then written
through the Native API's `b2_update_bucket` with the same application key, which needs the key's
writeBuckets capability; that is checked first.

Only the bucket's CORS configuration is read and written: never its ACL, policy, type, lifecycle
or objects, so the bucket stays private and every upload or download still needs a signed URL.
Output names origins, methods and outcomes only: never credentials, tokens, object keys or signed
URLs, so it is safe to run in a public GitHub Actions log.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx

    from datacourt.storage import S3ObjectStore

RULE_ID = "datacourt-browser-uploads"  # B2 rule names: 6-50 letters, digits and hyphens
METHODS = ("GET", "PUT", "HEAD")
REQUEST_HEADERS = ("content-type",)
EXPOSE_HEADERS = ("ETag",)
DEFAULT_MAX_AGE = 3600
PROBE_KEY = "datacourt-storage-check/cors-probe.zip"  # never written: preflights create nothing
FOREIGN_ORIGIN = "https://cors-probe.invalid"
B2_API = "https://api.backblazeb2.com"
_S3_OPERATIONS = {
    "GET": "s3_get",
    "HEAD": "s3_head",
    "PUT": "s3_put",
    "POST": "s3_post",
    "DELETE": "s3_delete",
}
_RULE_FIELDS = ("ID", "AllowedHeaders", "AllowedMethods", "AllowedOrigins", "ExposeHeaders", "MaxAgeSeconds")
_SAFELISTED_METHODS = frozenset({"GET", "HEAD", "POST"})
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_ORIGIN = re.compile(
    rf"^(?:https://{_LABEL}(?:\.{_LABEL})+|http://(?:localhost|127\.0\.0\.1))(?::[0-9]{{1,5}})?$"
)


class CorsError(RuntimeError):
    """A bucket request failed; the message is safe to print (no credentials, keys or URLs)."""


class NativeRulesPresent(CorsError):
    """B2 refuses PutBucketCors while the bucket has CORS rules set through its Native API."""


def normalize_origin(value: str) -> str:
    """An exact origin as browsers send it: `https://host[:port]` (plain http only for localhost),
    without wildcard, path, query or trailing slash."""
    origin = value.strip().lower()
    if not _ORIGIN.match(origin):
        raise ValueError(
            f"invalid origin {value.strip()[:100]!r}: use the exact scheme://host[:port] the app is "
            "served from, e.g. https://datacourt-ai.vercel.app (https only, no wildcard, path or "
            "trailing slash)"
        )
    return origin


def desired_rule(origins: Sequence[str], max_age: int = DEFAULT_MAX_AGE) -> dict[str, Any]:
    if not origins:
        raise ValueError("at least one origin is required")
    if not 0 <= max_age <= 86_400:
        raise ValueError("max age must be between 0 and 86400 seconds")
    return {
        "ID": RULE_ID,
        "AllowedOrigins": sorted({normalize_origin(o) for o in origins}),
        "AllowedMethods": list(METHODS),
        "AllowedHeaders": list(REQUEST_HEADERS),
        "ExposeHeaders": list(EXPOSE_HEADERS),
        "MaxAgeSeconds": max_age,
    }


def _clean(rule: Mapping[str, Any], n: int) -> dict[str, Any]:
    kept = {k: rule[k] for k in _RULE_FIELDS if rule.get(k) not in (None, [], "")}
    kept.setdefault("ID", f"kept-rule-{n}")
    return kept


def _plan(
    existing: Sequence[Mapping[str, Any]],
    desired: Mapping[str, Any],
    replace_all: bool,
    name_key: str,
    origins_key: str,
) -> list[dict[str, Any]]:
    ours = {o.lower() for o in desired[origins_key]}
    others = []
    for rule in [] if replace_all else existing:
        origins = [o for o in rule.get(origins_key) or [] if o.lower() not in ours]
        if rule.get(name_key) != desired[name_key] and origins:
            others.append({**rule, origins_key: origins})
    return [dict(desired), *others]


def plan_rules(
    existing: Sequence[Mapping[str, Any]], desired: Mapping[str, Any], replace_all: bool = False
) -> list[dict[str, Any]]:
    """Ours first, and the only rule for the app's origins: services differ in how they combine
    several rules matching one origin, so an older download-only rule for it (the web console's,
    say) must not be able to shadow the upload rule. Other rules keep their other origins."""
    ours, *others = _plan(existing, desired, replace_all, "ID", "AllowedOrigins")
    return [ours, *(_clean(r, n) for n, r in enumerate(others, start=1))]


def native_rule(rule: Mapping[str, Any]) -> dict[str, Any]:
    """The rule in the B2 Native API's form. Its operations are the S3 ones, so it governs exactly
    the requests the S3 rule would."""
    return {
        "corsRuleName": rule["ID"],
        "allowedOrigins": list(rule["AllowedOrigins"]),
        "allowedOperations": [_S3_OPERATIONS[m.upper()] for m in rule["AllowedMethods"]],
        "allowedHeaders": list(rule.get("AllowedHeaders") or []),
        "exposeHeaders": list(rule.get("ExposeHeaders") or []),
        "maxAgeSeconds": int(rule.get("MaxAgeSeconds", DEFAULT_MAX_AGE)),
    }


def plan_native_rules(
    existing: Sequence[Mapping[str, Any]], desired: Mapping[str, Any], replace_all: bool = False
) -> list[dict[str, Any]]:
    """`plan_rules` for rules in the B2 Native API's form; other rules are kept as they are."""
    return _plan(existing, desired, replace_all, "corsRuleName", "allowedOrigins")


def _norm(rule: Mapping[str, Any]) -> tuple:
    def values(key: str, upper: bool = False) -> tuple[str, ...]:
        return tuple(sorted({(v.upper() if upper else v.lower()) for v in rule.get(key) or []}))

    return (
        values("AllowedOrigins"),
        values("AllowedMethods", upper=True),
        values("AllowedHeaders"),
        values("ExposeHeaders"),
    )


def same_rule(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Same origins, methods and headers (case-insensitive). The max age is compared only when both
    state it, since a service may leave it out when reading rules back."""
    age_a, age_b = a.get("MaxAgeSeconds"), b.get("MaxAgeSeconds")
    return _norm(a) == _norm(b) and (age_a is None or age_b is None or int(age_a) == int(age_b))


def same_rules(a: Sequence[Mapping[str, Any]], b: Sequence[Mapping[str, Any]]) -> bool:
    return len(a) == len(b) and all(same_rule(x, y) for x, y in zip(a, b, strict=True))


def describe(rule: Mapping[str, Any]) -> str:
    def join(key: str) -> str:
        return ", ".join(rule.get(key) or []) or "-"

    return (
        f"{rule.get('ID') or '(unnamed)'}: origins {join('AllowedOrigins')}; methods "
        f"{join('AllowedMethods')}; request headers {join('AllowedHeaders')}; exposed "
        f"{join('ExposeHeaders')}; max age {rule.get('MaxAgeSeconds', '-')} s"
    )


def describe_native(rule: Mapping[str, Any]) -> str:
    def join(key: str) -> str:
        return ", ".join(rule.get(key) or []) or "-"

    return (
        f"{rule.get('corsRuleName') or '(unnamed)'}: origins {join('allowedOrigins')}; operations "
        f"{join('allowedOperations')}; request headers {join('allowedHeaders')}; exposed "
        f"{join('exposeHeaders')}; max age {rule.get('maxAgeSeconds', '-')} s"
    )


# ---------------------------------------------------------------------------
# Bucket requests
# ---------------------------------------------------------------------------


def _redacted(text: str, store: S3ObjectStore) -> str:
    for secret in (store.settings.s3_access_key_id, store.settings.s3_secret_access_key):
        if secret:
            text = text.replace(secret, "***")
    return re.sub(r"https?://\S+", "<url>", text)[:200]


def _failure(action: str, exc: Exception, store: S3ObjectStore) -> CorsError:
    from datacourt.storage import _error

    code, status, message = _error(exc)
    detail = f"{code or type(exc).__name__}" + (f" (HTTP {status})" if status else "")
    if message:
        detail += f": {_redacted(message, store)}"
    if code in {"AccessDenied", "Unauthorized", "Forbidden"} or status in (401, 403):
        capability = "writeBuckets" if action.startswith("write") else "readBuckets"
        detail += (
            f". The application key in S3_ACCESS_KEY_ID can use the bucket's files, but B2 needs "
            f"the {capability} capability to {action}; a key restricted to one bucket with "
            "'Read and Write' access from the web console may not have it. Nothing was changed. "
            "See docs/DEPLOYMENT.md, section 2, for the one-time alternatives."
        )
    return CorsError(f"could not {action}: {detail}")


def read_rules(store: S3ObjectStore) -> list[dict[str, Any]]:
    from botocore.exceptions import BotoCoreError, ClientError

    from datacourt.storage import _error

    try:
        r = store.client.get_bucket_cors(Bucket=store.bucket)
    except ClientError as exc:
        if _error(exc)[0] in {"NoSuchCORSConfiguration", "NoSuchCorsConfiguration"}:
            return []
        raise _failure("read the bucket's CORS rules", exc, store) from None
    except BotoCoreError as exc:
        raise CorsError(f"could not read the bucket's CORS rules: {type(exc).__name__}") from None
    return [dict(rule) for rule in r.get("CORSRules", [])]


def write_rules(store: S3ObjectStore, rules: Sequence[Mapping[str, Any]]) -> None:
    from botocore.exceptions import BotoCoreError, ClientError

    from datacourt.storage import _error

    try:
        store.client.put_bucket_cors(Bucket=store.bucket, CORSConfiguration={"CORSRules": list(rules)})
    except ClientError as exc:
        code, _status, message = _error(exc)
        if code == "InvalidRequest" and "native" in message.lower():
            raise NativeRulesPresent(
                "B2 refused it: the bucket has CORS rules set through the B2 Native API (the web "
                "console's presets are)"
            ) from None
        raise _failure("write the bucket's CORS rules", exc, store) from None
    except BotoCoreError as exc:
        raise CorsError(f"could not write the bucket's CORS rules: {type(exc).__name__}") from None


class B2Native:
    """The B2 Native API calls needed when B2 refuses PutBucketCors because of rules set through
    that API. Same application key as the S3 API. The only write is `b2_update_bucket` with the
    bucket's id and CORS rules: the bucket type, info, lifecycle and encryption are never sent, so
    they stay as they are. The authorization token and API URLs are never printed."""

    def __init__(self, http: httpx.Client, key_id: str, key: str, bucket: str, store: S3ObjectStore) -> None:
        self.http = http
        self.bucket_name = bucket
        self._store = store
        self._token = ""
        data = self._request(
            "authorize with the B2 Native API",
            "GET",
            f"{B2_API}/b2api/v2/b2_authorize_account",
            auth=(key_id, key),
        )
        self._token = str(data.get("authorizationToken") or "")
        self._api = str(data.get("apiUrl") or "")
        self.account_id = str(data.get("accountId") or "")
        allowed = data.get("allowed") or {}
        self.capabilities: list[str] = sorted(str(c) for c in allowed.get("capabilities") or [])
        if not (self._token and self._api and self.account_id):
            raise CorsError("could not authorize with the B2 Native API: unexpected response")

    def _redact(self, text: str) -> str:
        return _redacted(text.replace(self._token, "***") if self._token else text, self._store)

    def _request(self, action: str, method: str, url: str, **kw: Any) -> dict[str, Any]:
        import httpx

        try:
            r = self.http.request(method, url, **kw)
        except httpx.HTTPError as exc:  # its message may carry a URL: keep the class only
            raise CorsError(f"could not {action}: {type(exc).__name__}") from None
        try:
            data = r.json()
        except ValueError:
            data = None
        if r.status_code == 200 and isinstance(data, dict):
            return data
        err = data if isinstance(data, dict) else {}
        message = self._redact(str(err.get("message") or ""))
        raise CorsError(
            f"could not {action}: {err.get('code') or 'error'} (HTTP {r.status_code})"
            + (f": {message}" if message else "")
        )

    def _call(self, action: str, name: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            action, "POST", f"{self._api}/b2api/v2/{name}", headers={"Authorization": self._token}, json=body
        )

    def bucket(self) -> dict[str, Any]:
        found = self._call(
            "read the bucket (b2_list_buckets)",
            "b2_list_buckets",
            {"accountId": self.account_id, "bucketName": self.bucket_name},
        ).get("buckets")
        if not found:
            raise CorsError("the B2 Native API does not list the bucket for this application key")
        return dict(found[0])

    def set_cors_rules(self, bucket: Mapping[str, Any], rules: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        return self._call(
            "write the bucket's CORS rules (b2_update_bucket)",
            "b2_update_bucket",
            {"accountId": self.account_id, "bucketId": bucket["bucketId"], "corsRules": list(rules)},
        )


# ---------------------------------------------------------------------------
# Preflight probes: the OPTIONS requests a browser sends before its uploads and downloads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Probe:
    name: str
    method: str
    headers: tuple[str, ...] = ()


UPLOAD_PROBES = (
    Probe("single-file upload (PUT with Content-Type)", "PUT", REQUEST_HEADERS),
    Probe("multipart part upload (PUT)", "PUT"),
)
DOWNLOAD_PROBES = (Probe("download (GET)", "GET"), Probe("download (HEAD)", "HEAD"))


@dataclass(frozen=True)
class Preflight:
    status: int
    allow_origin: str = ""
    allow_methods: frozenset[str] = frozenset()
    allow_headers: frozenset[str] = frozenset()
    error: str = ""  # the request itself failed (exception class name only)

    def problem(self, origin: str, probe: Probe) -> str:
        """Why a browser would block the request, checked the way browsers check it ("" if not)."""
        if self.error:
            return f"no response ({self.error})"
        if self.status >= 400:
            return f"HTTP {self.status}"
        if self.allow_origin not in (origin, "*"):
            return f"HTTP {self.status} without Access-Control-Allow-Origin for this origin"
        if probe.method not in self.allow_methods and probe.method not in _SAFELISTED_METHODS:
            return f"HTTP {self.status} but Access-Control-Allow-Methods lacks {probe.method}"
        missing = sorted({h.lower() for h in probe.headers} - self.allow_headers)
        if missing and "*" not in self.allow_headers:
            return f"HTTP {self.status} but Access-Control-Allow-Headers lacks {', '.join(missing)}"
        return ""

    def allows(self, origin: str, probe: Probe) -> bool:
        return not self.problem(origin, probe)


def _tokens(value: str, upper: bool = False) -> frozenset[str]:
    parts = (p.strip() for p in value.split(","))
    return frozenset(p.upper() if upper else p.lower() for p in parts if p)


def preflight(http: httpx.Client, url: str, origin: str, probe: Probe) -> Preflight:
    import httpx

    headers = {"Origin": origin, "Access-Control-Request-Method": probe.method}
    if probe.headers:
        headers["Access-Control-Request-Headers"] = ",".join(probe.headers)
    try:
        r = http.options(url, headers=headers)
    except httpx.HTTPError as exc:  # its message may carry the signed URL: keep the class only
        return Preflight(0, error=type(exc).__name__)
    return Preflight(
        r.status_code,
        r.headers.get("access-control-allow-origin", ""),
        _tokens(r.headers.get("access-control-allow-methods", ""), upper=True),
        _tokens(r.headers.get("access-control-allow-headers", "")),
    )


def probe_urls(store: S3ObjectStore) -> dict[str, str]:
    """Presigned URLs like the ones the app hands to browsers. Signing is local; preflight requests
    carry no credentials and create nothing."""
    return {
        "PUT": str(store.signed_put(PROBE_KEY, 600, "application/zip", 10)["url"]),
        "GET": store.signed_get_url(PROBE_KEY, 600),
        "HEAD": store.signed_get_url(PROBE_KEY, 600),
    }


def run_probes(
    http: httpx.Client, urls: Mapping[str, str], origin: str, probes: Sequence[Probe]
) -> list[tuple[Probe, Preflight]]:
    return [(p, preflight(http, urls[p.method], origin, p)) for p in probes]


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


@dataclass
class Step:
    name: str
    outcome: str  # pass | fail | warn | info
    detail: str = ""


@dataclass
class CorsReport:
    provider: str
    origins: list[str]
    dry_run: bool
    before: list[dict[str, Any]] = field(default_factory=list)
    planned: list[dict[str, Any]] = field(default_factory=list)
    after: list[dict[str, Any]] | None = None
    native_before: list[dict[str, Any]] | None = None  # set when written through the B2 Native API
    native_written: list[dict[str, Any]] | None = None
    changed: bool = False
    unchanged: bool = False  # the bucket already had exactly these rules
    steps: list[Step] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(s.outcome == "fail" for s in self.steps)


def _verify(report: CorsReport, http: httpx.Client, urls: Mapping[str, str], wait_seconds: float) -> None:
    """Required: every browser request from every origin. Rule changes can take a little while
    to reach every B2 server, so refused preflights are retried until `wait_seconds` pass."""
    probes = (*UPLOAD_PROBES, *DOWNLOAD_PROBES)
    t0 = time.monotonic()
    delay = 2.0
    while True:
        results = {o: run_probes(http, urls, o, probes) for o in report.origins}
        passed = all(pf.allows(o, p) for o, rs in results.items() for p, pf in rs)
        remaining = wait_seconds - (time.monotonic() - t0)
        if passed or remaining <= 0:
            break
        time.sleep(min(delay, remaining))
        delay = min(delay * 2, 15.0)
    waited = f" after {time.monotonic() - t0:.0f}s" if passed and time.monotonic() - t0 >= 1 else ""
    for origin, rs in results.items():
        for probe, pf in rs:
            ok = pf.allows(origin, probe)
            report.steps.append(
                Step(
                    f"preflight from {origin}: {probe.name}",
                    "pass" if ok else ("info" if report.dry_run else "fail"),
                    f"accepted{waited}" if ok else f"refused: {pf.problem(origin, probe)}",
                )
            )
    foreign = run_probes(http, urls, FOREIGN_ORIGIN, UPLOAD_PROBES[:1])[0][1]
    if foreign.error:
        outcome, detail = "warn", f"not checked: {foreign.problem(FOREIGN_ORIGIN, UPLOAD_PROBES[0])}"
    elif foreign.allows(FOREIGN_ORIGIN, UPLOAD_PROBES[0]):
        outcome = "warn"
        detail = (
            "accepted: another rule on the bucket allows uploads from any origin; rerun with "
            "--replace-all to keep only the app's rule"
        )
    else:
        outcome, detail = "pass", f"refused: {foreign.problem(FOREIGN_ORIGIN, UPLOAD_PROBES[0])}"
    report.steps.append(Step("upload preflight from another origin is refused", outcome, detail))


def _write_native(
    report: CorsReport,
    store: S3ObjectStore,
    http: httpx.Client,
    desired: Mapping[str, Any],
    replace_all: bool,
) -> None:
    settings = store.settings
    b2 = B2Native(
        http, settings.s3_access_key_id or "", settings.s3_secret_access_key or "", store.bucket or "", store
    )
    if "writeBuckets" not in b2.capabilities:
        raise CorsError(
            "the application key lacks the writeBuckets capability that changing bucket settings "
            f"needs (it has: {', '.join(b2.capabilities) or 'none'}). Nothing was changed. See "
            "docs/DEPLOYMENT.md, section 2, to set the rule once with the B2 command-line tool"
        )
    report.steps.append(Step("application key may change bucket settings", "pass", "writeBuckets present"))
    bucket = b2.bucket()
    report.native_before = [dict(r) for r in bucket.get("corsRules") or []]
    planned = plan_native_rules(report.native_before, native_rule(desired), replace_all)
    updated = b2.set_cors_rules(bucket, planned)
    report.native_written = planned
    stored = [r.get("corsRuleName") for r in updated.get("corsRules") or []]
    expected = [r.get("corsRuleName") for r in planned]
    report.steps.append(
        Step(
            "write rules (B2 Native API b2_update_bucket, CORS rules only)",
            "pass" if stored == expected else "warn",
            f"{len(planned)} rule(s)"
            if stored == expected
            else "the bucket reports other rules than written",
        )
    )
    kind = str(updated.get("bucketType") or "unknown")
    report.steps.append(
        Step("bucket is still private", "pass" if kind == "allPrivate" else "warn", f"bucket type {kind}")
    )


def configure(
    origins: Sequence[str],
    *,
    dry_run: bool = False,
    replace_all: bool = False,
    max_age: int = DEFAULT_MAX_AGE,
    wait_seconds: float = 90.0,
    store: S3ObjectStore | None = None,
    http: httpx.Client | None = None,
) -> CorsReport:
    import httpx

    from datacourt.config import get_settings
    from datacourt.storage import S3ObjectStore, provider_name

    desired = desired_rule(origins, max_age)  # validates the origins before any request
    if store is None:
        settings = get_settings()
        if settings.storage_backend != "s3":
            raise CorsError("STORAGE_BACKEND must be s3 (with the S3_* settings) to configure bucket CORS")
        store = S3ObjectStore(settings)
    report = CorsReport(provider_name(store.settings.s3_endpoint), desired["AllowedOrigins"], dry_run)
    own_http = http is None
    client = http or httpx.Client(timeout=30.0, follow_redirects=False)
    try:
        report.before = read_rules(store)
        report.steps.append(
            Step("read current rules (GetBucketCors)", "pass", f"{len(report.before)} rule(s)")
        )
        report.planned = plan_rules(report.before, desired, replace_all)
        if dry_run:
            report.steps.append(Step("write rules", "info", "dry run: nothing changed"))
        elif same_rules(report.before, report.planned):
            report.unchanged = True
            report.steps.append(Step("write rules", "pass", "already up to date: not rewritten"))
        else:
            try:
                write_rules(store, report.planned)
                report.steps.append(
                    Step("write rules (PutBucketCors)", "pass", f"{len(report.planned)} rule(s)")
                )
            except NativeRulesPresent as exc:
                report.steps.append(
                    Step(
                        "write rules (PutBucketCors)",
                        "info",
                        f"{exc}; writing the same rule through that API with the same key",
                    )
                )
                _write_native(report, store, client, desired, replace_all)
            report.changed = True
        if not dry_run:
            # The preflights below decide; a rule the service stores in another form only warns.
            report.after = read_rules(store)
            present = any(same_rule(r, desired) for r in report.after)
            report.steps.append(
                Step(
                    "rule read back from the bucket",
                    "pass" if present else "warn",
                    "present" if present else "listed differently from what was written (see above)",
                )
            )
        _verify(report, client, probe_urls(store), 0.0 if dry_run else wait_seconds)
    except CorsError as exc:
        report.steps.append(Step("bucket CORS", "fail", str(exc)))
    finally:
        if own_http:
            client.close()
    return report


def _print(report: CorsReport) -> None:
    suffix = " - DRY RUN" if report.dry_run else ""
    print(f"Bucket CORS ({report.provider}) for {', '.join(report.origins)}{suffix}")
    print(f"Current rules ({len(report.before)}):")
    for rule in report.before:
        print(f"  - {describe(rule)}")
    if report.native_written is not None:
        print(f"Rules as the B2 Native API listed them ({len(report.native_before or [])}):")
        for rule in report.native_before or []:
            print(f"  - {describe_native(rule)}")
        print(f"Rules written through the B2 Native API ({len(report.native_written)}):")
        for rule in report.native_written:
            print(f"  - {describe_native(rule)}")
    elif report.planned:
        if report.dry_run:
            verb = "Rules to write"
        elif report.changed:
            verb = "Rules written"
        else:
            verb = "Rules (already in place)" if report.unchanged else "Rules NOT written"
        print(f"{verb} ({len(report.planned)}):")
        for rule in report.planned:
            print(f"  - {describe(rule)}")
    if report.after is not None and not same_rules(report.after, report.planned):
        print(f"Rules the bucket lists now ({len(report.after)}):")
        for rule in report.after:
            print(f"  - {describe(rule)}")
    for s in report.steps:
        print(f"  [{s.outcome.upper()}] {s.name}{' - ' + s.detail if s.detail else ''}")
    if report.dry_run:
        print("Dry run finished; nothing was changed.")
    else:
        print("Browser uploads are allowed." if report.ok else "Bucket CORS is NOT configured correctly.")


def _summary(report: CorsReport, path: str) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"### Bucket CORS: {report.provider}{' (dry run)' if report.dry_run else ''}\n\n")
        fh.write("| step | result |\n|---|---|\n")
        for s in report.steps:
            fh.write(f"| {s.name} | {s.outcome}{': ' + s.detail if s.detail else ''} |\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="datacourt-storage-cors", description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--origin",
        action="append",
        required=True,
        help="exact app origin browsers upload from, e.g. https://datacourt-ai.vercel.app (repeatable)",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="show the current and planned rules; change nothing"
    )
    ap.add_argument("--replace-all", action="store_true", help="drop every other CORS rule on the bucket")
    ap.add_argument("--max-age", type=int, default=DEFAULT_MAX_AGE, help="preflight cache seconds (0-86400)")
    ap.add_argument("--wait", type=float, default=90.0, help="seconds to wait for a new rule to take effect")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    try:
        report = configure(
            args.origin,
            dry_run=args.dry_run,
            replace_all=args.replace_all,
            max_age=args.max_age,
            wait_seconds=args.wait,
        )
    except (ValueError, CorsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"ok": report.ok, **asdict(report)}))
    else:
        _print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        _summary(report, summary)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
