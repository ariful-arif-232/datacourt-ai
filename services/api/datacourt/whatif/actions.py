"""What-if change-set vocabulary and validation (import-light: used by the API).

The experiment itself (`datacourt.whatif.lab`) needs scikit-learn and runs in a worker.
"""

from __future__ import annotations

ACTION_TYPES = {
    "remove_samples",
    "relabel_samples",
    "remove_duplicate_copies",
    "exclude_quality",
    "rebalance",
    "preserve_rare",
    "move_leakage_out_of_eval",
    "apply_review_decisions",
    "apply_jury_suggestions",
}
SEV_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3}
RISK_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


class InvalidChangeSet(ValueError):
    pass


def validate_actions(actions: list[dict]) -> None:
    if not actions:
        raise InvalidChangeSet("a change set needs at least one action")
    if len(actions) > 20:
        raise InvalidChangeSet("too many actions (max 20)")
    for a in actions:
        if a.get("type") not in ACTION_TYPES:
            raise InvalidChangeSet(f"unknown action type {a.get('type')!r}")
        if a["type"] == "remove_samples" and not isinstance(a.get("sample_ids"), list):
            raise InvalidChangeSet("remove_samples needs sample_ids")
        if a["type"] == "relabel_samples" and not isinstance(a.get("items"), list):
            raise InvalidChangeSet("relabel_samples needs items [{sample_id, target_class}]")
        if a["type"] == "rebalance" and not (
            isinstance(a.get("max_per_class"), int) or isinstance(a.get("class_balanced"), bool)
        ):
            raise InvalidChangeSet("rebalance needs max_per_class (int) or class_balanced (bool)")
