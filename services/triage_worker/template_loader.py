"""Hot-reloadable YAML template loader for the cascading triage worker (R6.13, R6.14).

Loads and compiles approved response templates from declarative YAML configuration
files with mtime-based hot-reloading and fail-safe error isolation.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from packages.domain.templates import TemplateDefinition, TemplateRegistry

logger = logging.getLogger(__name__)


def load_templates_from_yaml(
    yaml_content: str,
    base_dir: Path | str | None = None,
) -> TemplateRegistry:
    """Parse declarative templates from a YAML string and return a TemplateRegistry.

    Raises:
        ValueError: If YAML is malformed, not a mapping, or lacks a 'templates' list.
    """
    try:
        data = yaml.safe_load(yaml_content)
    except Exception as exc:
        raise ValueError(f"Failed to parse YAML content: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("Invalid template configuration: top-level YAML must be a mapping")

    if "templates" not in data or not isinstance(data["templates"], list):
        raise ValueError("Invalid template configuration: missing 'templates' list in YAML")

    return TemplateRegistry.from_dict(data)


def load_templates_from_file(path: str | Path) -> TemplateRegistry:
    """Read a YAML template file from disk and return a compiled TemplateRegistry."""
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Template configuration file not found: {file_path}")

    content = file_path.read_text(encoding="utf-8")
    return load_templates_from_yaml(content, base_dir=file_path.parent)


class HotReloadableTemplateRegistry:
    """Thread-safe and fail-safe hot-reloadable template registry wrapper.

    Monitors file modification time (mtime) and automatically re-compiles templates
    on access. If an updated file contains invalid syntax or errors, the registry logs
    an error and retains the previous valid templates without crashing.
    """

    def __init__(
        self,
        config_path: str | Path,
        base_dir: Path | str | None = None,
    ) -> None:
        self.config_path = Path(config_path)
        self.base_dir = Path(base_dir) if base_dir else self.config_path.parent
        self._last_mtime: float = 0.0
        self._registry: TemplateRegistry = TemplateRegistry()

        if self.config_path.exists():
            self._reload()
        else:
            logger.warning("Template config file %s does not exist on init.", self.config_path)

    def _reload(self) -> None:
        """Attempt to read and compile templates from config file."""
        try:
            mtime = self.config_path.stat().st_mtime
            if mtime <= self._last_mtime and self._registry.templates:
                return

            logger.info(
                "Loading template configuration from %s (mtime=%s)", self.config_path, mtime
            )
            content = self.config_path.read_text(encoding="utf-8")
            new_registry = load_templates_from_yaml(content, base_dir=self.base_dir)

            self._registry = new_registry
            self._last_mtime = mtime
            logger.info("Successfully loaded %d response templates", len(new_registry.templates))
        except Exception as exc:
            logger.error(
                "Failed to reload templates from %s: %s. Keeping existing templates.",
                self.config_path,
                exc,
            )

    @property
    def registry(self) -> TemplateRegistry:
        """Return the active TemplateRegistry, hot-reloading if file changed on disk."""
        if self.config_path.exists():
            try:
                current_mtime = self.config_path.stat().st_mtime
                if current_mtime > self._last_mtime:
                    self._reload()
            except OSError as exc:
                logger.warning("Error checking template config mtime: %s", exc)

        return self._registry

    def find_template(self, category: str, intent: str | None) -> TemplateDefinition | None:
        """Delegate lookup to the active template registry."""
        return self.registry.find_template(category, intent)
