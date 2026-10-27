"""Playground settings (design.md, ``settings.py``).

Every path defaults from ``repo_root``. On the host ``repo_root`` is the repository root
(computed from this file's location); in Docker the service sets ``PLAYGROUND__REPO_ROOT=/app``.
API keys are deliberately not settings: ``core/models.py`` reads them from the environment
at call time so they never sit in an object that gets rendered.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_L1_MODEL_PATH = Path("C:/AgentMailGuard-bench-artifacts/l1_injection_clf_v1.joblib")

# field name -> path relative to repo_root (guard-relative paths are handled separately).
_REPO_RELATIVE: dict[str, tuple[str, ...]] = {
    "guard_root": ("agentmailguard",),
    "cases_path": ("evaluation", "datasets", "mailguard", "cases.jsonl"),
    "results_root": ("evaluation", "results", "mailguard_bench"),
    "figures_dir": ("paper", "figures", "final", "3models"),
    "data_dir": ("services", "guard_playground", "data"),
}


class PlaygroundSettings(BaseSettings):
    """Configuration of the Playground_Service; env prefix ``PLAYGROUND__``.

    No env file is configured, so ``PlaygroundSettings()`` and
    ``PlaygroundSettings(_env_file=None)`` behave the same and the host ``.env`` never leaks in.
    Paths not given explicitly (kwargs or env) are derived from ``repo_root``.
    """

    model_config = SettingsConfigDict(env_prefix="PLAYGROUND__", env_nested_delimiter="__")

    host: str = "127.0.0.1"
    port: int = 3010
    # False when started by the Fallback_Launcher (-ReplayOnly).
    live_enabled: bool = True
    repo_root: Path = DEFAULT_REPO_ROOT
    # The defaults below are placeholders: _derive_paths always fills them from repo_root.
    guard_root: Path = Path()
    cases_path: Path = Path()
    fixtures_path: Path = Path()
    results_root: Path = Path()
    figures_dir: Path = Path()
    data_dir: Path = Path()
    l1_model_path: Path = DEFAULT_L1_MODEL_PATH
    run_timeout_s: float = 120.0
    batch_concurrency: int = 2
    max_upload_bytes: int = 5 * 1024 * 1024
    max_batch_emails: int = 50

    @model_validator(mode="before")
    @classmethod
    def _derive_paths(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        root = Path(data.get("repo_root") or DEFAULT_REPO_ROOT)
        for name, parts in _REPO_RELATIVE.items():
            if not data.get(name):
                data[name] = root.joinpath(*parts)
        if not data.get("fixtures_path"):
            data["fixtures_path"] = Path(data["guard_root"]) / "tests" / "fixtures" / "emails.json"
        return data
