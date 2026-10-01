"""Runtime-image smoke check, piped into the built image by `make image-smoke` (RA.11).

Runs inside the image (not copied into it): proves pytest is absent, every entrypoint
imports, and every runtime asset the code loads resolves from WORKDIR /app.
"""

import importlib
import importlib.util
from pathlib import Path

assert importlib.util.find_spec("pytest") is None, "pytest must not be installed in the image"

for module in (
    "services.api.main",
    "services.frontend.main",
    "services.email_worker.main",
    "services.knowledge_worker.main",
    "services.triage_worker.main",
    "services.ai_worker.main",
    "services.dispatch_worker.main",
    "services.mail_connector.main",
    "packages.broker.cli",
    "packages.db.cli",
    "packages.core.storage_cli",
):
    importlib.import_module(module)

from packages.broker.routing import load_categories_from_yaml  # noqa: E402
from packages.core.settings import AppSettings  # noqa: E402
from packages.db.migrator import discover_migrations  # noqa: E402
from packages.llm.profile import AgentProfileRegistry  # noqa: E402
from services.triage_worker.classifier import MLClassifier  # noqa: E402
from services.triage_worker.rules import load_rules_from_file  # noqa: E402
from services.triage_worker.template_loader import load_templates_from_file  # noqa: E402

settings = AppSettings()
assert discover_migrations(), "no SQL migrations found next to packages/db/migrator.py"
MLClassifier.load_from_artifact(settings.triage.ml_model_path)
load_rules_from_file(settings.triage.rules_path)
assert load_categories_from_yaml(settings.routing.categories_config_path), "no categories"
registry = AgentProfileRegistry.from_yaml(settings.agent_profiles.config_path)
for profile in registry.profiles:
    registry.get_schema(profile)
    assert Path(profile.prompt_template).is_file(), profile.prompt_template
templates = load_templates_from_file(settings.triage.templates_path)
for template in templates.templates:
    if template.body.endswith((".txt", ".j2", ".md")):
        assert Path(template.body).is_file(), template.body
from packages.knowledge.token_counter import TokenCounter  # noqa: E402

# Run with --network none: the BPE encoding must already be in the image (4.13b).
assert TokenCounter()._encoding is not None, "tiktoken encoding is not baked into the image"
print("image smoke OK")
