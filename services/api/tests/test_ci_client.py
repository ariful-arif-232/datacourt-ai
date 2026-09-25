from datacourt import ci_client

RESULT = {
    "passed": False,
    "contract": {
        "name": "DataCourt default contract",
        "version": 0,
        "passed": False,
        "results": [
            {
                "metric": "exact_cross_split_duplicates",
                "title": "Exact duplicates",
                "op": "<=",
                "expected": 0,
                "actual": 3,
                "passed": False,
            },
            {
                "metric": "min_images_per_class",
                "title": "Smallest class size",
                "op": ">=",
                "expected": 30,
                "actual": 41,
                "passed": True,
            },
        ],
    },
    "preflight": {"status": "BLOCKED", "effective_status": "BLOCKED", "blocking": ["exact_eval_leakage"]},
    "debt": "HIGH",
    "dataset_version_id": "x",
}


def test_exit_codes_and_rendering(monkeypatch, capsys):
    monkeypatch.setenv("DATACOURT_TOKEN", "dct_test")
    monkeypatch.setattr(ci_client, "fetch", lambda *a, **k: RESULT)
    assert ci_client.main(["--api", "https://example.test/api/v1", "--dataset-version", "v"]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] Exact duplicates: 3 <= 0" in out and "[PASS] Smallest class size" in out
    assert "exact_eval_leakage" in out and out.strip().endswith("FAILED")

    monkeypatch.setattr(ci_client, "fetch", lambda *a, **k: {**RESULT, "passed": True})
    assert ci_client.main(["--api", "https://example.test/api/v1", "--dataset-version", "v"]) == 0


def test_usage_errors(monkeypatch):
    monkeypatch.delenv("DATACOURT_TOKEN", raising=False)
    assert ci_client.main(["--api", "https://example.test/api/v1", "--dataset-version", "v"]) == 2
    monkeypatch.setenv("DATACOURT_TOKEN", "dct_test")
    assert ci_client.main(["--api", "file:///etc/passwd", "--dataset-version", "v"]) == 2
