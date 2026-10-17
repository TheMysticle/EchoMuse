"""The Echo's own wake word decoder is an owwModel value with no file behind it."""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import em_oww_assets as A
import em_oww_models as M


def test_the_engine_is_device_only_and_nothing_else_is():
    assert M.is_device_only("pryon_alexa")
    assert M.is_device_only("  pryon_alexa ")
    for other in ("alexa_v0.1", "hey_jarvis_v0.1", "/data/oww_models/pryon_alexa.onnx", "pryon", ""):
        assert not M.is_device_only(other), other


def test_the_controller_scores_it_with_the_same_word_from_the_open_models():
    assert M.controller_model("pryon_alexa") == "alexa_v0.1"
    # everything else is untouched, including values the helper must not "fix"
    for v in ("alexa_v0.1", "/data/x/custom.onnx", "hey_mycroft_v0.1"):
        assert M.controller_model(v) == v


def test_home_assistant_is_told_the_wake_word_is_alexa():
    # wake_word_phrase and the advertised name come from the same function, and
    # must agree whichever engine the Echo is running.
    assert M.display_name("pryon_alexa") == M.display_name("alexa_v0.1") == "alexa"
    assert M.prediction_key("pryon_alexa") == "alexa_v0.1"


def test_other_names_key_exactly_as_before():
    assert M.prediction_key("hey_jarvis_v0.1") == "hey_jarvis_v0.1"
    assert M.prediction_key("/data/oww_models/scooby.onnx") == "scooby"


def test_recorded_model_key_preserves_device_only_engine():
    assert M.recorded_model_key("pryon_alexa") == "pryon_alexa"
    assert M.recorded_model_key("hey_jarvis_v0.1") == "hey_jarvis_v0.1"
    assert M.recorded_model_key("/data/oww_models/scooby.onnx") == "scooby"


def test_the_asset_planner_looks_for_the_stand_in_not_a_file_called_pryon(tmp_path):
    res = tmp_path / "res"
    res.mkdir()
    (res / "alexa_v0.1.onnx").write_bytes(b"x")
    assert A.classifier_source("pryon_alexa", res, None) == res / "alexa_v0.1.onnx"
    assert A.classifier_source("alexa_v0.1", res, None) == res / "alexa_v0.1.onnx"
    assert A.classifier_source("unknown_v0.1", res, None) is None


def test_selecting_the_engine_makes_the_stand_in_the_pinned_selected_classifier(tmp_path):
    res = tmp_path / "res"
    res.mkdir()
    for n in ("alexa_v0.1", "hey_jarvis_v0.1"):
        (res / f"{n}.onnx").write_bytes(n.encode())
    assets, problems = A.desired_assets(["pryon_alexa"], runtime_dir=tmp_path, resources=res,
                                        models_dir=tmp_path, include_stock=True)
    classifiers = [a.name for a in assets if a.kind == "classifier"]
    assert classifiers[0] == "alexa_v0.1.onnx", "the selected model must lead: it is the one checked and pinned"
    assert not any("pryon" in p for p in problems), problems
    assert "pryon_alexa.onnx" not in classifiers


def test_an_echo_without_the_engine_is_not_told_to_use_it():
    # em_api cannot be imported by the suite; hold the ordering in the source.
    src = (pathlib.Path(__file__).resolve().parents[1] / "em_api.py").read_text()
    body = src[src.index("def _hold_back_oww_model"):src.index("async def _install_then_switch")]
    assert "em_oww_models.is_device_only(new_model)" in body
    assert body.index("is_device_only") > body.index("if not (was_local or now_local)"), \
        "controller-scored Echoes are unaffected: the check comes after that gate"
    assert "live.wake_pryon_capable" in body
    cap = (pathlib.Path(__file__).resolve().parents[1] / "em_controller.py").read_text()
    assert '"wake_pryon" in (self.capabilities or [])' in cap
