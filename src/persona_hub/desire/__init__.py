"""Desire system: drives that decide when the persona wants to do something on its own."""

from .context import DesireContextUnavailable, build_desire_messages
from .host import ActivityRecord, BeatResult, DesireHost, OutputGuard
from .kernel import DEFAULT_CATALOG, ActionCatalog, DesireState, Gates, Intent

__all__ = [
    "DEFAULT_CATALOG",
    "ActionCatalog",
    "ActivityRecord",
    "BeatResult",
    "DesireContextUnavailable",
    "DesireHost",
    "DesireState",
    "Gates",
    "Intent",
    "OutputGuard",
    "build_desire_messages",
]
