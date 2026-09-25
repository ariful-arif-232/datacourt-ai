from datacourt.services.explain import build_evidence_payload


def test_llm_payload_excludes_private_fields():
    case = {
        "case_number": 7,
        "verdict": "REVIEW",
        "reason_codes": ["MODEL_DISAGREEMENT"],
        "uncertainty": "medium",
    }
    evidence = [
        {
            "witness": "model",
            "stance": "prosecution",
            "evidence_kind": "model_prediction",
            "title": "Baseline disagrees",
            "detail": "p=0.2",
            "value": {
                "p_given": 0.2,
                "path": "train/cat/x.jpg",
                "sha256": "ab" * 32,
                "note": "private",
                "big": list(range(50)),
            },
        }
    ]
    payload = build_evidence_payload(case, evidence, {"label": "cat", "split": "train"})
    values = payload["evidence"][0]["values"]
    assert values == {"p_given": 0.2}
    assert "x.jpg" not in str(payload)
