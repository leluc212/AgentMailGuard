"""Dataset provenance, download and build tooling (public benchmarks + seed corpus).

The directory ``datasets/`` at the project root holds *data* (raw / processed / seed);
this package holds the *code* that produces it, so that nothing shadows the
Hugging Face ``datasets`` library.
"""

from mailguard.datasets.sources import SOURCES, DatasetSource, by_role

__all__ = ["SOURCES", "DatasetSource", "by_role"]
