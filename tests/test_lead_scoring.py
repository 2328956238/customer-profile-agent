import json
import math
from pathlib import Path

import pytest

from customer_profile_agent.lead_scoring import (
    EXAMPLE_FEATURES, FEATURE_NAMES, LABELS, LeadScorer,
    build_demo_dataset, dataset_sha256, main, train_demo_model, validate_features,
)


@pytest.fixture(scope="module")
def trained():
    return train_demo_model()


def test_group_holdout_has_no_same_customer_snapshots(trained):
    _, report = trained
    dataset = build_demo_dataset()
    train = report["split"]["train"]
    holdout = report["split"]["holdout"]
    assert set(train["customer_ids"]).isdisjoint(holdout["customer_ids"])
    assert report["split"]["group_overlap"] == []
    assert train["groups"] + holdout["groups"] == report["dataset"]["groups"] == 144
    assert train["samples"] + holdout["samples"] == len(dataset["rows"]) == 288
    for subset in (train, holdout):
        selected = [row for row in dataset["rows"] if row["customer_id"] in subset["customer_ids"]]
        assert len(selected) == subset["samples"] == 2 * subset["groups"]
        assert set(row["label"] for row in selected) == set(LABELS)
        assert sum(subset["class_counts"].values()) == subset["samples"]
        assert sum(subset["class_group_counts"].values()) == subset["groups"]


def test_preprocessing_is_fitted_on_training_groups_only(trained):
    scorer, report = trained
    dataset = build_demo_dataset()
    training_ids = set(report["split"]["train"]["customer_ids"])
    selected = [row for row in dataset["rows"] if row["customer_id"] in training_ids]
    for index, name in enumerate(FEATURE_NAMES):
        values = [row["features"][name] for row in selected]
        if name in ("message_count", "requested_quantity"):
            values = [math.log1p(value) for value in values]
        mean = sum(values) / len(values)
        scale = math.sqrt(sum((value - mean) ** 2 for value in values) / len(values)) or 1
        assert scorer.to_dict()["mean"][index] == pytest.approx(mean)
        assert scorer.to_dict()["scale"][index] == pytest.approx(scale)
    assert report["preprocessing"]["scaler_fit_scope"] == "training_groups_only"


def test_same_data_seed_and_environment_repeat_identically(trained):
    scorer, report = trained
    repeated, repeated_report = train_demo_model()
    assert repeated_report == report
    assert repeated.to_dict() == scorer.to_dict()
    assert dataset_sha256(build_demo_dataset()) == report["dataset_sha256"]
    changed = build_demo_dataset()
    changed["rows"][0]["features"]["asks_price"] = not changed["rows"][0]["features"]["asks_price"]
    assert dataset_sha256(changed) != report["dataset_sha256"]


def test_three_models_report_all_classes_and_real_confusion_counts(trained):
    scorer, report = trained
    assert set(report["models"]) == {"logistic_regression", "dummy_most_frequent", "random_forest"}
    assert report["serving_model"] == "logistic_regression"
    holdout = report["split"]["holdout"]
    for metrics in report["models"].values():
        assert set(metrics["per_class"]) == set(LABELS)
        assert sum(map(sum, metrics["confusion_matrix"])) == holdout["samples"]
        assert 0 <= metrics["macro_f1"] <= 1
        for index, label in enumerate(LABELS):
            assert metrics["per_class"][label]["support"] == holdout["class_counts"][label]
            assert sum(metrics["confusion_matrix"][index]) == holdout["class_counts"][label]
    # Independent reconstruction catches wrong class ordering in JSON deployment.
    confusion = [[0] * 3 for _ in LABELS]
    for row in build_demo_dataset()["rows"]:
        if row["customer_id"] in holdout["customer_ids"]:
            prediction = scorer.predict(row["features"])
            confusion[LABELS.index(row["label"])][LABELS.index(prediction["label"])] += 1
    assert confusion == report["models"]["logistic_regression"]["confusion_matrix"]


def test_json_round_trip_and_advisory_contract(trained, tmp_path):
    scorer, _ = trained
    artifact = tmp_path / "model.json"
    scorer.save(artifact)
    loaded = LeadScorer.load(artifact)
    prediction = loaded.predict(EXAMPLE_FEATURES)
    assert prediction == scorer.predict(EXAMPLE_FEATURES)
    assert sum(prediction["scores"].values()) == pytest.approx(1.0)
    assert prediction["model_score"] == max(prediction["scores"].values())
    assert prediction["label"] in LABELS
    assert prediction["calibrated"] is False and prediction["advisory_only"] is True
    assert "不是成交概率" in prediction["explanation"]
    assert "不替代" in prediction["explanation"]
    assert "handoff" not in prediction


@pytest.mark.parametrize("field,value", [
    ("message_count", 0), ("message_count", True), ("message_count", 1.0),
    ("requested_quantity", -1), ("requested_quantity", 1000001),
    ("requested_quantity", "50"), ("requested_quantity", float("nan")),
    ("specified_attribute_count", 5), ("asks_price", 1),
    ("has_destination", "yes"), ("has_purchase_timeframe", None),
])
def test_invalid_feature_types_or_ranges_are_rejected(field, value):
    features = dict(EXAMPLE_FEATURES, **{field: value})
    with pytest.raises(ValueError):
        validate_features(features)


@pytest.mark.parametrize("field", ["converted", "payment_received", "handoff_done", "followup_result", "customer_id"])
def test_outcomes_and_customer_ids_cannot_enter_feature_matrix(field, trained):
    scorer, _ = trained
    with pytest.raises(ValueError, match="exactly"):
        scorer.predict(dict(EXAMPLE_FEATURES, **{field: 1}))
    dataset = build_demo_dataset()
    dataset["rows"][0]["features"][field] = 1
    with pytest.raises(ValueError, match="exactly"):
        train_demo_model(dataset)


def test_missing_feature_is_not_silently_imputed(trained):
    scorer, _ = trained
    features = dict(EXAMPLE_FEATURES)
    del features["has_purchase_timeframe"]
    with pytest.raises(ValueError):
        scorer.predict(features)


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 2), ("model_type", "pickle"),
    ("coefficients", [[1.0]]), ("mean", [0.0]),
    ("scale", [0.0] * 7), ("intercept", [float("inf")] * 3),
    ("intercept", [10 ** 400] * 3),
    ("calibrated", True), ("advisory_only", False), ("synthetic_training", False),
    ("feature_names", list(reversed(FEATURE_NAMES))),
    ("labels", list(reversed(LABELS))), ("dataset_sha256", "unknown"),
])
def test_malformed_or_misleading_model_artifacts_rejected(trained, field, value):
    scorer, _ = trained
    artifact = scorer.to_dict()
    artifact[field] = value
    with pytest.raises(ValueError):
        LeadScorer(artifact)


def test_artifact_is_copied_and_cannot_load_executable_fields(trained):
    scorer, _ = trained
    artifact = scorer.to_dict()
    loaded = LeadScorer(artifact)
    before = loaded.predict(EXAMPLE_FEATURES)
    artifact["coefficients"][0][0] = 999
    artifact["mean"][0] = 999
    assert loaded.predict(EXAMPLE_FEATURES) == before
    with pytest.raises(ValueError):
        LeadScorer(dict(scorer.to_dict(), python_callable="anything"))


def test_load_rejects_oversized_duplicate_and_non_json_artifacts(tmp_path):
    path = tmp_path / "untrusted.json"
    for raw in (b"x" * 65537, b'{"schema_version": 1, "schema_version": 1}', b"\x80\x04pickle"):
        path.write_bytes(raw)
        with pytest.raises(ValueError):
            LeadScorer.load(path)


def test_trainer_rejects_unlabeled_real_or_duplicate_snapshots():
    dataset = build_demo_dataset()
    dataset["synthetic"] = False
    with pytest.raises(ValueError, match="explicitly synthetic"):
        train_demo_model(dataset)
    dataset = build_demo_dataset()
    dataset["rows"][1]["snapshot_id"] = dataset["rows"][0]["snapshot_id"]
    with pytest.raises(ValueError, match="duplicate"):
        train_demo_model(dataset)
    dataset = build_demo_dataset()
    dataset["rows"][0]["label"] = "sold"
    with pytest.raises(ValueError, match="unknown label"):
        train_demo_model(dataset)


def test_committed_fixture_is_exactly_the_documented_synthetic_generator():
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "leads" / "synthetic-leads.json"
    assert json.loads(fixture.read_text(encoding="utf-8")) == build_demo_dataset()


def test_offline_cli_writes_actual_report_and_portable_model(tmp_path, capsys, monkeypatch):
    import socket

    def deny_network(*args, **kwargs):
        raise AssertionError("offline training must not use the network")

    monkeypatch.setattr(socket, "create_connection", deny_network)
    report_path, model_path = tmp_path / "report.json", tmp_path / "model.json"
    dataset_path = tmp_path / "dataset.json"
    main(["--report", str(report_path), "--model", str(model_path), "--write-dataset", str(dataset_path)])
    printed = json.loads(capsys.readouterr().out)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    model = LeadScorer.load(model_path)
    assert dataset_sha256(dataset) == report["dataset_sha256"] == printed["dataset_sha256"]
    assert printed["example"] == model.predict(EXAMPLE_FEATURES)
