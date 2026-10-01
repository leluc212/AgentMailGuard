"""Reranker packaging contract (R11.1, R11.5; task 7.20).

sentence-transformers needs torch, and PyPI's Linux torch wheel is the CUDA build: several GB of
nvidia-* wheels a CPU-only ai-worker never uses. uv must take torch from PyTorch's CPU index, and
the image must ship the model so the ai-worker needs no network at runtime. CI has no docker
build step, so this test parses pyproject.toml, uv.lock and the Dockerfile instead.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from packages.core.settings import RetrievalSettings
from packages.retrieval.rerank import CrossEncoderReranker

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"
CPU_INDEX = "https://download.pytorch.org/whl/cpu"
CUDA_ONLY_PACKAGES = {"triton", "cuda-bindings", "cuda-toolkit", "cuda-pathfinder"}


def _pyproject() -> dict[str, Any]:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _locked_packages() -> list[dict[str, Any]]:
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages: list[dict[str, Any]] = lock["package"]
    return packages


def _locked_sources(name: str) -> list[dict[str, Any]]:
    """Every lock entry for ``name``: torch has one per local-version family (+cpu and macOS)."""
    return [package["source"] for package in _locked_packages() if package["name"] == name]


def _declared_dependencies() -> set[str]:
    requirements = _pyproject()["project"]["dependencies"]
    return {re.split(r"[\s\[<>=!~;]", req, maxsplit=1)[0].lower() for req in requirements}


def _dockerfile_value(pattern: str) -> str:
    match = re.search(pattern, DOCKERFILE.read_text(encoding="utf-8"), flags=re.MULTILINE)
    assert match is not None, f"Dockerfile has no line matching {pattern!r}"
    return match.group(1)


def test_sentence_transformers_and_torch_are_declared_runtime_dependencies() -> None:
    """torch is declared directly: a uv source only applies to a direct dependency."""
    assert {"sentence-transformers", "torch"} <= _declared_dependencies()


def test_torch_is_sourced_from_an_explicit_cpu_only_index() -> None:
    uv = _pyproject()["tool"]["uv"]
    index = next(i for i in uv["index"] if i["url"] == CPU_INDEX)
    assert index["explicit"] is True, "an implicit index would also serve jinja2, numpy, ..."
    assert uv["sources"]["torch"] == [{"index": index["name"]}]


def test_lock_resolves_torch_from_the_cpu_index_without_cuda_wheels() -> None:
    torch_sources = _locked_sources("torch")
    assert torch_sources, "torch is not in uv.lock"
    assert all(source == {"registry": CPU_INDEX} for source in torch_sources), torch_sources
    assert _locked_sources("sentence-transformers") == [{"registry": "https://pypi.org/simple"}]
    names = {package["name"] for package in _locked_packages()}
    cuda = sorted(n for n in names if n.startswith("nvidia-") or n in CUDA_ONLY_PACKAGES)
    assert not cuda, f"the lock pulls the CUDA build of torch: {cuda}"


def test_image_bakes_the_rerank_model_where_the_worker_looks_for_it() -> None:
    """R11.5: no download at runtime, so a container without network still reranks.

    The build runs the same CrossEncoder load the worker does (cache_folder), so the cache
    layout cannot drift from what CrossEncoderReranker reads.
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    model_dir = _dockerfile_value(r"^ENV RETRIEVAL__RERANK_MODEL_DIR=(\S+)$")
    load = next(
        line for line in text.splitlines() if line.startswith("RUN ") and "CrossEncoder(" in line
    )
    assert "/app/.venv/bin/python" in load
    assert "cache_folder='${RETRIEVAL__RERANK_MODEL_DIR}'" in load
    assert "CrossEncoder('${RERANK_MODEL}'" in load
    assert text.index("ENV RETRIEVAL__RERANK_MODEL_DIR=") < text.index(load)
    assert model_dir.startswith("/app/")


def test_rerank_model_layer_survives_code_changes() -> None:
    """The 90 MB download sits after the locked dependencies and before the source COPYs.

    It needs only the venv, and a layer below COPY packages/ would download the model again
    on every code change.
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    load = next(
        line for line in text.splitlines() if line.startswith("RUN ") and "CrossEncoder(" in line
    )
    deps = text.index("RUN uv sync --locked --no-dev --no-install-project")
    assert deps < text.index(load) < text.index("COPY packages/")


def test_the_three_copies_of_the_default_model_agree() -> None:
    """Settings own the default; the image bakes it and the reranker falls back to it."""
    default = RetrievalSettings().rerank_model
    assert _dockerfile_value(r"^ARG RERANK_MODEL=(\S+)$") == default
    assert CrossEncoderReranker().model_name == default
