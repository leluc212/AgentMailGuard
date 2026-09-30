"""The benchmark's config scheme: v1 (the published names) and v2 (the 2026-09-30 configs).

Pure code, no AgentMailGuard import: CI covers it. ADR-0012 decision 11; v2 design Amendment 2.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluation.mailguard_bench import scheme

V2_FLAGS = {
    "C0T": (),
    "C1": ("l1", "l5"),
    "C2": ("l2", "l5"),
    "C3": ("l3", "l5"),
    "C4": ("l3b", "l5"),
    "C5": ("l4", "l5"),
    "C6": ("l5",),
    "C7": ("l1", "l2", "l3", "l3b", "l4", "l5"),
}


def test_the_two_schemes_and_the_default() -> None:
    assert scheme.SCHEMES == ("v1", "v2")
    assert scheme.DEFAULT_SCHEME == "v2"  # a new run is v2 unless it asks for v1


def test_the_v2_configs_and_their_layer_flags_are_the_pre_registered_ones() -> None:
    assert scheme.V2_CONFIGS == ("C0", "C0T", "C1", "C2", "C3", "C4", "C5", "C6", "C7")
    assert scheme.V2_LAYERS["C0"] == ()  # rag-email's own path: no guard at all
    for config, layers in V2_FLAGS.items():
        assert scheme.V2_LAYERS[config] == layers, config
    assert scheme.V2_TARGET_CONFIG == "C7"
    assert scheme.V1_TARGET_CONFIG == "C3"


def test_the_v1_configs_keep_their_names_and_the_ablation_stays_v1_only() -> None:
    assert scheme.V1_CONFIGS == ("C0", "C0T", "C1", "C2", "C3")
    assert scheme.ABLATION_CONFIGS == ("C3-L1", "C3-L2", "C3-L3", "C3-L3B", "C3-L4", "C3-L5")
    assert scheme.configs_for("v1") == (*scheme.V1_CONFIGS, *scheme.ABLATION_CONFIGS)
    assert scheme.configs_for("v2") == scheme.V2_CONFIGS
    assert not set(scheme.ABLATION_CONFIGS) & set(scheme.configs_for("v2"))


def test_the_live_ai_stages_per_v2_config() -> None:
    # The owner's list: C1 the L1 judge; C2 L2's AI step; C3 none; C4 L3b's AI stage; C5 L4's
    # AI stage; C6 none; C7 all four.
    assert scheme.V2_AI_STAGES == {
        "C0": (),
        "C0T": (),
        "C1": ("l1.judge",),
        "C2": ("l2.llm",),
        "C3": (),
        "C4": ("l3b.llm",),
        "C5": ("l4.llm",),
        "C6": (),
        "C7": ("l1.judge", "l2.llm", "l3b.llm", "l4.llm"),
    }


def test_the_l3b_and_l4_llm_switches_follow_the_ai_stages() -> None:
    assert {c: scheme.v2_llm_stages(c) for c in scheme.V2_CONFIGS} == {
        "C0": (False, False),
        "C0T": (False, False),
        "C1": (False, False),
        "C2": (False, False),
        "C3": (False, False),
        "C4": (True, False),
        "C5": (False, True),
        "C6": (False, False),
        "C7": (True, True),
    }


def test_the_stages_a_v2_config_needs_live_include_the_classifier_where_a_layer_reads_it() -> None:
    # L1, L2 and L3b read the trained classifier; L3, L4 and L5 do not.
    assert {c: scheme.v2_required_stages(c) for c in scheme.V2_CONFIGS} == {
        "C0": (),
        "C0T": (),
        "C1": ("l1.classifier", "l1.judge"),
        "C2": ("l1.classifier", "l2.llm"),
        "C3": (),
        "C4": ("l1.classifier", "l3b.llm"),
        "C5": ("l4.llm",),
        "C6": (),
        "C7": ("l1.classifier", "l1.judge", "l2.llm", "l3b.llm", "l4.llm"),
    }


def test_a_v2_guard_config_has_a_name_of_its_own_and_none_for_the_native_path() -> None:
    assert scheme.v2_guard_name("C1") == "v2-C1"
    assert scheme.v2_guard_name("C0T") == "v2-C0T"
    with pytest.raises(ValueError, match="native"):
        scheme.v2_guard_name("C0")
    with pytest.raises(ValueError, match="C3-L1"):
        scheme.v2_guard_name("C3-L1")


def test_the_ai_layer_names_a_v2_config_reports_fallbacks_for() -> None:
    assert scheme.v2_ai_layer_names("C1") == ("l1_injection_scanner",)
    assert scheme.v2_ai_layer_names("C3") == ()
    assert scheme.v2_ai_layer_names("C7") == (
        "l1_injection_scanner",
        "l2_intent_extractor",
        "l3b_document_scanner",
        "l4_output_scanner",
    )


@pytest.mark.parametrize(
    ("scheme_name", "config"),
    [("v1", "C3-L1"), ("v1", "C3"), ("v2", "C7"), ("v2", "C0")],
)
def test_a_config_of_the_scheme_is_accepted(scheme_name: str, config: str) -> None:
    scheme.require_config(scheme_name, config)


@pytest.mark.parametrize(
    ("scheme_name", "config"),
    [("v1", "C4"), ("v1", "C7"), ("v2", "C3-L1"), ("v2", "C8"), ("v2", "")],
)
def test_a_config_of_the_other_scheme_is_refused_with_the_scheme_named(
    scheme_name: str, config: str
) -> None:
    with pytest.raises(ValueError, match=f"scheme {scheme_name}"):
        scheme.require_config(scheme_name, config)


def test_a_meta_without_a_scheme_is_v1() -> None:
    assert scheme.scheme_of_meta({}) == "v1"
    assert scheme.scheme_of_meta(None) == "v1"
    assert scheme.scheme_of_meta({"scheme": "v1"}) == "v1"
    assert scheme.scheme_of_meta({"scheme": "v2"}) == "v2"
    with pytest.raises(ValueError, match="v3"):
        scheme.scheme_of_meta({"scheme": "v3"})


def test_the_target_config_and_the_full_guard_follow_the_scheme() -> None:
    assert scheme.target_config("v1") == "C3"
    assert scheme.target_config("v2") == "C7"


def _meta(run: Path, name: str, body: dict[str, object] | str) -> None:
    (run / "raw").mkdir(parents=True, exist_ok=True)
    text = body if isinstance(body, str) else json.dumps(body)
    (run / "raw" / f"{name}.meta.json").write_text(text, encoding="utf-8")


def test_an_empty_or_new_folder_has_no_scheme_yet(tmp_path: Path) -> None:
    assert scheme.folder_scheme(tmp_path / "missing") is None
    (tmp_path / "raw").mkdir()
    assert scheme.folder_scheme(tmp_path) is None
    scheme.require_folder_scheme(tmp_path, "v1")
    scheme.require_folder_scheme(tmp_path, "v2")


def test_a_folder_of_v1_metas_without_a_scheme_is_v1(tmp_path: Path) -> None:
    _meta(tmp_path, "C0", {"preset": "C0"})
    _meta(tmp_path, "C3", {"preset": "C3", "scheme": "v1"})
    assert scheme.folder_scheme(tmp_path) == "v1"
    scheme.require_folder_scheme(tmp_path, "v1")


def test_a_run_never_mixes_schemes_in_one_folder(tmp_path: Path) -> None:
    _meta(tmp_path, "C0", {"preset": "C0"})  # no scheme: v1
    with pytest.raises(scheme.SchemeMixError, match="v1") as caught:
        scheme.require_folder_scheme(tmp_path, "v2")
    assert "C0.meta.json" in str(caught.value)
    assert "new RUN" in str(caught.value)

    other = tmp_path / "other"
    _meta(other, "C7", {"scheme": "v2"})
    with pytest.raises(scheme.SchemeMixError, match="v2"):
        scheme.require_folder_scheme(other, "v1")


def test_the_guard_workers_meta_counts_too(tmp_path: Path) -> None:
    _meta(tmp_path, "guard_worker.C7", {"scheme": "v2"})
    assert scheme.folder_scheme(tmp_path) == "v2"
    with pytest.raises(scheme.SchemeMixError):
        scheme.require_folder_scheme(tmp_path, "v1")


def test_a_folder_that_already_mixes_is_refused_when_read(tmp_path: Path) -> None:
    _meta(tmp_path, "C0", {"scheme": "v1"})
    _meta(tmp_path, "C7", {"scheme": "v2"})
    with pytest.raises(scheme.SchemeMixError, match="mixes"):
        scheme.folder_scheme(tmp_path)


def test_a_torn_meta_file_is_not_a_scheme(tmp_path: Path) -> None:
    _meta(tmp_path, "C0", "{not json")
    _meta(tmp_path, "C1", {"scheme": "v2"})
    assert scheme.folder_scheme(tmp_path) == "v2"
