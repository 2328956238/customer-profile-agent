"""Offline, synthetic lead-priority classification; never a conversion forecast.

Training uses sklearn. JSON inference uses only the standard library and never
loads executable model artifacts. Business handoff rules remain independent.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import random
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

DATASET_SEED = 20261015
SPLIT_SEED = 4107
MODEL_SEED = 4139
LABELS = ("low", "medium", "high")
FEATURE_NAMES = (
    "message_count", "specified_attribute_count", "requested_quantity",
    "has_destination", "asks_price", "asks_shipping", "has_purchase_timeframe",
)
LABEL_DEFINITIONS = {
    "low": "合成场景：初步了解产品，需求尚不具体",
    "medium": "合成场景：比较规格与条件，需求逐渐具体",
    "high": "合成场景：较具体的近期采购询盘，建议优先查看",
}
EXPLANATION = (
    "分数是合成标注意向类别的未校准模型分数，不是成交概率；"
    "仅供客服安排查看优先级，不替代人工判断、客户转人工请求或业务交接规则。"
)
EXAMPLE_FEATURES = {
    "message_count": 8,
    "specified_attribute_count": 3,
    "requested_quantity": 50,
    "has_destination": True,
    "asks_price": True,
    "asks_shipping": True,
    "has_purchase_timeframe": True,
}
_NUMERIC_RANGES = {
    "message_count": (1, 200),
    "specified_attribute_count": (0, 4),
    "requested_quantity": (0, 1000000),
}
_ARTIFACT_KEYS = {
    "schema_version", "model_type", "feature_names", "labels", "transform",
    "mean", "scale", "coefficients", "intercept", "dataset_sha256",
    "calibrated", "advisory_only", "synthetic_training",
}
_MAX_ARTIFACT_BYTES = 65536


def validate_features(features: Mapping[str, Any]) -> dict[str, Any]:
    """Reject IDs, outcomes, missing values and implicit type coercion."""
    if not isinstance(features, Mapping) or set(features) != set(FEATURE_NAMES):
        raise ValueError("features must contain exactly the seven declared decision-time fields")
    result = dict(features)
    for name in FEATURE_NAMES:
        value = result[name]
        if name in _NUMERIC_RANGES:
            low, high = _NUMERIC_RANGES[name]
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} must be an integer in [{low}, {high}]")
        elif type(value) is not bool:
            raise ValueError(f"{name} must be a boolean")
    return result


def _vector(features: Mapping[str, Any]) -> list[float]:
    clean = validate_features(features)
    return [
        math.log1p(clean[name]) if name in ("message_count", "requested_quantity")
        else float(clean[name])
        for name in FEATURE_NAMES
    ]


def build_demo_dataset() -> dict[str, Any]:
    """Build fictional labeled snapshots without using contacts or outcome data.

    Labels are authored scenario categories, not retrospectively observed sales.
    Conditional distributions intentionally overlap. The shared scenario recipe
    makes this a mechanics demonstration, not independent real-world validation.
    """
    rng = random.Random(DATASET_SEED)
    rows = []
    settings = {
        "low": ((1, 8), (0, 0, 1, 1, 2, 3), (0, 0, 0, 5, 10, 20, 50), (.25, .75, .20, .15)),
        "medium": ((2, 12), (1, 1, 2, 2, 3, 4), (0, 5, 10, 20, 50, 100), (.50, .85, .55, .35)),
        "high": ((3, 16), (1, 2, 3, 3, 4, 4), (0, 10, 20, 50, 100, 200), (.80, .80, .80, .75)),
    }
    for index in range(144):
        label = LABELS[index % len(LABELS)]
        counts, attributes, quantities, probabilities = settings[label]
        customer_id = f"demo-customer-{index + 1:03d}"
        base = {
            "message_count": rng.randint(*counts),
            "specified_attribute_count": rng.choice(attributes),
            "requested_quantity": rng.choice(quantities),
            **{name: rng.random() < probability
               for name, probability in zip(FEATURE_NAMES[3:], probabilities)},
        }
        for snapshot in range(2):
            features = dict(base)
            if snapshot:
                features["message_count"] += rng.randint(1, 3)
                features["specified_attribute_count"] = max(
                    0, min(4, features["specified_attribute_count"] + rng.choice((-1, 0, 0, 1)))
                )
                # Later snapshot is still a pre-decision inquiry, never a sales result.
                if rng.random() < .2:
                    features["has_purchase_timeframe"] = not features["has_purchase_timeframe"]
            rows.append({
                "customer_id": customer_id,
                "snapshot_id": f"{customer_id}-inquiry-{snapshot + 1}",
                "label": label,
                "features": features,
            })
    return {
        "schema_version": 1,
        "synthetic": True,
        "dataset_seed": DATASET_SEED,
        "provenance": "2026-10-15 公开脱敏展示版新增；程序生成的虚构客户与人工定义场景标签，无真实客户或成交记录。",
        "label_definitions": LABEL_DEFINITIONS.copy(),
        "rows": rows,
    }


def dataset_sha256(dataset: dict[str, Any]) -> str:
    encoded = json.dumps(dataset, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validate_dataset(dataset: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(dataset, dict) or dataset.get("schema_version") != 1 or dataset.get("synthetic") is not True:
        raise ValueError("this demo trainer accepts only explicitly synthetic schema-v1 data")
    rows = dataset.get("rows")
    if not isinstance(rows, list) or not 30 <= len(rows) <= 10000:
        raise ValueError("synthetic dataset must have 30..10000 labeled snapshots")
    snapshot_ids = set()
    customer_labels: dict[str, set[str]] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"customer_id", "snapshot_id", "label", "features"}:
            raise ValueError("unexpected dataset row fields")
        for field in ("customer_id", "snapshot_id"):
            if not isinstance(row[field], str) or not row[field].startswith("demo-customer-") or len(row[field]) > 100:
                raise ValueError("dataset IDs must be explicit fictional demo-customer identifiers")
        if row["snapshot_id"] in snapshot_ids:
            raise ValueError("duplicate snapshot_id")
        snapshot_ids.add(row["snapshot_id"])
        if row["label"] not in LABELS:
            raise ValueError("unknown label")
        validate_features(row["features"])
        customer_labels.setdefault(row["customer_id"], set()).add(row["label"])
    if any(sum(label in labels for labels in customer_labels.values()) < 4 for label in LABELS):
        raise ValueError("each label needs at least four different synthetic customer groups")
    return rows


def _finite_vector(value: Any, size: int, name: str, *, positive: bool = False) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) != size:
        raise ValueError(f"invalid {name} shape")
    for number in value:
        if (type(number) not in (int, float) or abs(number) > 1000000
                or not math.isfinite(number) or (positive and number <= 0)):
            raise ValueError(f"invalid {name} values")
    return tuple(float(number) for number in value)


class LeadScorer:
    """Frozen-by-copy logistic coefficients with a strict, non-executable format."""

    def __init__(self, artifact: dict[str, Any]):
        if not isinstance(artifact, dict) or set(artifact) != _ARTIFACT_KEYS:
            raise ValueError("unexpected model artifact fields")
        if (type(artifact["schema_version"]) is not int or artifact["schema_version"] != 1
                or artifact["model_type"] != "multinomial_logistic_regression"
                or artifact["feature_names"] != list(FEATURE_NAMES)
                or artifact["labels"] != list(LABELS)
                or artifact["transform"] != "log1p_message_count_and_requested_quantity_then_standard_scaler"
                or artifact["calibrated"] is not False or artifact["advisory_only"] is not True
                or artifact["synthetic_training"] is not True):
            raise ValueError("unsupported model artifact contract")
        digest = artifact["dataset_sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("invalid training data hash")
        self._dataset_hash = digest
        self._mean = _finite_vector(artifact["mean"], len(FEATURE_NAMES), "mean")
        self._scale = _finite_vector(artifact["scale"], len(FEATURE_NAMES), "scale", positive=True)
        self._intercept = _finite_vector(artifact["intercept"], len(LABELS), "intercept")
        matrix = artifact["coefficients"]
        if not isinstance(matrix, list) or len(matrix) != len(LABELS):
            raise ValueError("invalid coefficients shape")
        self._coefficients = tuple(_finite_vector(row, len(FEATURE_NAMES), "coefficients") for row in matrix)

    def predict(self, features: Mapping[str, Any]) -> dict[str, Any]:
        vector = _vector(features)
        normalized = [(v - mean) / scale for v, mean, scale in zip(vector, self._mean, self._scale)]
        logits = [bias + sum(c * v for c, v in zip(row, normalized))
                  for row, bias in zip(self._coefficients, self._intercept)]
        if not all(math.isfinite(value) for value in logits):
            raise ValueError("artifact produced non-finite inference values")
        exponentials = [math.exp(value - max(logits)) for value in logits]
        scores = dict(zip(LABELS, (value / sum(exponentials) for value in exponentials)))
        label = max(LABELS, key=lambda item: scores[item])
        return {
            "label": label, "model_score": scores[label], "scores": scores,
            "calibrated": False, "advisory_only": True, "explanation": EXPLANATION,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "model_type": "multinomial_logistic_regression",
            "feature_names": list(FEATURE_NAMES),
            "labels": list(LABELS),
            "transform": "log1p_message_count_and_requested_quantity_then_standard_scaler",
            "mean": list(self._mean), "scale": list(self._scale),
            "coefficients": [list(row) for row in self._coefficients],
            "intercept": list(self._intercept), "dataset_sha256": self._dataset_hash,
            "calibrated": False, "advisory_only": True, "synthetic_training": True,
        }

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "LeadScorer":
        with Path(path).open("rb") as stream:
            raw = stream.read(_MAX_ARTIFACT_BYTES + 1)
        if len(raw) > _MAX_ARTIFACT_BYTES:
            raise ValueError("model artifact is too large")

        def unique_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate model artifact keys")
                result[key] = value
            return result

        try:
            artifact = json.loads(raw, object_pairs_hook=unique_keys)
        except (UnicodeDecodeError, RecursionError) as exc:
            raise ValueError("invalid JSON model artifact") from exc
        return cls(artifact)


def train_demo_model(dataset: dict[str, Any] | None = None) -> tuple[LeadScorer, dict[str, Any]]:
    """Fit three fixed baselines on training groups, then evaluate once on holdout.

    Logistic regression is chosen before evaluation for simple coefficient-only
    deployment. Holdout results do not select hyperparameters or the winner.
    """
    import numpy as np
    import scipy
    import sklearn
    from sklearn.dummy import DummyClassifier
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import classification_report, confusion_matrix
    from sklearn.model_selection import GroupShuffleSplit
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    data = build_demo_dataset() if dataset is None else dataset
    rows = _validate_dataset(data)
    matrix = np.array([_vector(row["features"]) for row in rows], dtype=float)
    labels = np.array([row["label"] for row in rows])
    groups = np.array([row["customer_id"] for row in rows])
    splitter = GroupShuffleSplit(n_splits=1, test_size=.25, random_state=SPLIT_SEED)
    train_indices, holdout_indices = next(splitter.split(matrix, labels, groups))
    train_indices, holdout_indices = np.sort(train_indices), np.sort(holdout_indices)
    for indices in (train_indices, holdout_indices):
        if set(labels[indices]) != set(LABELS):
            raise ValueError("fixed group split must contain all classes; do not cherry-pick the seed")
    models = {
        "logistic_regression": Pipeline([
            ("scale", StandardScaler()),
            ("classifier", LogisticRegression(C=1.0, solver="lbfgs", max_iter=1000, random_state=MODEL_SEED)),
        ]),
        "dummy_most_frequent": DummyClassifier(strategy="most_frequent"),
        "random_forest": RandomForestClassifier(
            n_estimators=100, max_depth=4, min_samples_leaf=4,
            max_features="sqrt", n_jobs=1, random_state=MODEL_SEED,
        ),
    }
    results = {}
    for name, estimator in models.items():
        estimator.fit(matrix[train_indices], labels[train_indices])
        prediction = estimator.predict(matrix[holdout_indices])
        metrics = classification_report(labels[holdout_indices], prediction, labels=list(LABELS), output_dict=True, zero_division=0)
        results[name] = {
            "per_class": {label: {
                "precision": round(metrics[label]["precision"], 8),
                "recall": round(metrics[label]["recall"], 8),
                "f1": round(metrics[label]["f1-score"], 8),
                "support": int(metrics[label]["support"]),
            } for label in LABELS},
            "macro_f1": round(metrics["macro avg"]["f1-score"], 8),
            "accuracy": round(metrics["accuracy"], 8),
            "confusion_matrix": confusion_matrix(labels[holdout_indices], prediction, labels=list(LABELS)).tolist(),
        }
    pipeline = models["logistic_regression"]
    scaler, logistic = pipeline.named_steps["scale"], pipeline.named_steps["classifier"]
    class_indices = [list(logistic.classes_).index(label) for label in LABELS]
    digest = dataset_sha256(data)
    scorer = LeadScorer({
        "schema_version": 1, "model_type": "multinomial_logistic_regression",
        "feature_names": list(FEATURE_NAMES), "labels": list(LABELS),
        "transform": "log1p_message_count_and_requested_quantity_then_standard_scaler",
        "mean": scaler.mean_.tolist(), "scale": scaler.scale_.tolist(),
        "coefficients": logistic.coef_[class_indices].tolist(),
        "intercept": logistic.intercept_[class_indices].tolist(), "dataset_sha256": digest,
        "calibrated": False, "advisory_only": True, "synthetic_training": True,
    })
    # Verify the portable scorer against sklearn on every held-out snapshot.
    for index, expected in zip(holdout_indices, pipeline.predict_proba(matrix[holdout_indices])):
        scores = scorer.predict(rows[int(index)]["features"])["scores"]
        if not np.allclose([scores[label] for label in LABELS], expected[class_indices], rtol=1e-10, atol=1e-12):
            raise RuntimeError("portable JSON scorer differs from the trained sklearn model")

    def summarize(indices):
        counts = Counter(str(label) for label in labels[indices])
        unique_groups = sorted(set(str(group) for group in groups[indices]))
        return {
            "samples": len(indices), "groups": len(unique_groups),
            "class_counts": {label: counts[label] for label in LABELS},
            "class_group_counts": {label: len(set(groups[indices][labels[indices] == label])) for label in LABELS},
            "customer_ids": unique_groups,
        }

    report = {
        "schema_version": 1,
        "scope": "2026-10-15 公开脱敏展示版新增；仅合成数据离线分类实验，不是生产业绩或成交概率验证。",
        "dataset_sha256": digest,
        "dataset": {"samples": len(rows), "groups": len(set(groups)), "classes": len(LABELS), "synthetic": True},
        "feature_names": list(FEATURE_NAMES), "label_order": list(LABELS),
        "label_definitions": LABEL_DEFINITIONS.copy(),
        "split": {
            "method": "GroupShuffleSplit", "group_field": "customer_id",
            "seed": SPLIT_SEED, "holdout_fraction": .25,
            "train": summarize(train_indices), "holdout": summarize(holdout_indices),
            "group_overlap": sorted(set(groups[train_indices]) & set(groups[holdout_indices])),
        },
        "preprocessing": {
            "log1p": ["message_count", "requested_quantity"],
            "scaler_fit_scope": "training_groups_only",
            "mean": scaler.mean_.tolist(), "scale": scaler.scale_.tolist(),
        },
        "model_parameters": {
            "logistic_regression": {"C": 1.0, "solver": "lbfgs", "max_iter": 1000, "random_state": MODEL_SEED},
            "dummy_most_frequent": {"strategy": "most_frequent"},
            "random_forest": {"n_estimators": 100, "max_depth": 4, "min_samples_leaf": 4,
                              "max_features": "sqrt", "n_jobs": 1, "random_state": MODEL_SEED},
        },
        "models": results,
        "serving_model": "logistic_regression",
        "selection_policy": "Chosen before holdout evaluation; no tuning or model selection on holdout.",
        "versions": {"python": platform.python_version(), "scikit_learn": sklearn.__version__, "numpy": np.__version__, "scipy": scipy.__version__},
        "limitations": [
            "All labels and features come from shared synthetic scenario recipes; this is not an independent real-customer test.",
            "Group separation prevents same-customer snapshot leakage, not scenario-template bias or future distribution shift.",
            "No sales, payment, follow-up, handoff outcome, identity or customer_id enters the feature matrix.",
            "Scores are uncalibrated class scores, not conversion probabilities. No production quality claim is supported.",
            "This auxiliary score must never gate a customer request for a human or override deterministic handoff rules.",
        ],
    }
    return scorer, report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Train synthetic inquiry-priority baselines offline; no network or customer data.")
    parser.add_argument("--dataset", type=Path, help="optional explicitly synthetic JSON dataset")
    parser.add_argument("--write-dataset", type=Path, help="write the generated fictional dataset")
    parser.add_argument("--report", type=Path, help="write the actual evaluation report")
    parser.add_argument("--model", type=Path, help="write a non-executable JSON model")
    args = parser.parse_args(argv)
    dataset = json.loads(args.dataset.read_text(encoding="utf-8")) if args.dataset else build_demo_dataset()
    scorer, report = train_demo_model(dataset)
    for path, value in ((args.write_dataset, dataset), (args.report, report)):
        if path:
            path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    if args.model:
        scorer.save(args.model)
    print(json.dumps({
        "dataset_sha256": report["dataset_sha256"], "dataset": report["dataset"],
        "train_samples": report["split"]["train"]["samples"],
        "holdout_samples": report["split"]["holdout"]["samples"],
        "macro_f1": {name: metrics["macro_f1"] for name, metrics in report["models"].items()},
        "example": scorer.predict(EXAMPLE_FEATURES),
        "limitations": report["limitations"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
