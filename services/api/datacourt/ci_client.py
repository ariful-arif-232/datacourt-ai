"""`datacourt-ci`: fail a CI job when a dataset version breaks its data contract.

Standard library only, so it runs on any CI image with Python 3.11+:

    DATACOURT_TOKEN=dct_... datacourt-ci --api https://datacourt.example.com/api/v1 \\
        --dataset-version 3f1c...

Exit codes: 0 contract passed, 1 contract failed or preflight BLOCKED, 2 usage/network/API error.
The token needs the `contracts:read` scope and is read from the environment, never from argv.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def fetch(api: str, version_id: str, token: str, timeout: float) -> dict:
    if urllib.parse.urlparse(api).scheme not in ("https", "http"):
        raise ValueError("API URL must use http(s)")
    url = f"{api.rstrip('/')}/ci/contract?{urllib.parse.urlencode({'dataset_version_id': version_id})}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    req = urllib.request.Request(url, headers=headers)  # noqa: S310 - scheme validated above
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - scheme validated above
        return json.loads(resp.read().decode("utf-8"))


def render(result: dict) -> str:
    lines = [
        f"DataCourt data contract: {result['contract']['name']} v{result['contract']['version']}",
        f"Preflight: {result['preflight']['effective_status']}  ·  Dataset Debt: {result['debt']}",
        "",
    ]
    for r in result["contract"]["results"]:
        mark = "PASS" if r["passed"] else "FAIL"
        lines.append(
            f"  [{mark}] {r['title']}: {json.dumps(r['actual'])} {r['op']} {json.dumps(r['expected'])}"
        )
    if result["preflight"]["blocking"]:
        lines.append(f"  [FAIL] Preflight blocking checks: {', '.join(result['preflight']['blocking'])}")
    lines += ["", "PASSED" if result["passed"] else "FAILED"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="datacourt-ci", description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--api", default=os.environ.get("DATACOURT_API_URL"), help="API base URL ending in /api/v1"
    )
    ap.add_argument(
        "--dataset-version", default=os.environ.get("DATACOURT_DATASET_VERSION_ID"), required=False
    )
    ap.add_argument("--json", action="store_true", help="print the raw JSON result")
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args(argv)
    token = os.environ.get("DATACOURT_TOKEN")
    if not args.api or not args.dataset_version or not token:
        print("datacourt-ci: --api, --dataset-version and DATACOURT_TOKEN are required", file=sys.stderr)
        return 2
    if urllib.parse.urlparse(args.api).scheme not in ("https", "http"):
        print("datacourt-ci: --api must be an http(s) URL", file=sys.stderr)
        return 2
    try:
        result = fetch(args.api, args.dataset_version, token, args.timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        print(f"datacourt-ci: API returned {exc.code}: {detail}", file=sys.stderr)
        return 2
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"datacourt-ci: request failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
