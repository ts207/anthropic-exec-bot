from __future__ import annotations

# Transitional shared import surface. These dataclasses are generic, but still
# live in polybot.iran.config until the next extraction phase.
from polybot.iran.config import ClassifierConfig, SafetyConfig, SourcesConfig

__all__ = ["ClassifierConfig", "SafetyConfig", "SourcesConfig"]

