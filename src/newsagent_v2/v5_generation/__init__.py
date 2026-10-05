"""V5 Generation Pipeline.

Connects V5 NewsEvent → RUN STORY → Existing Generation Engine → Review → Publication.

Exports are lazy so importing one submodule does not load the rest.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "RunStoryAdapter",
    "VersionStore",
    "ReviewSystem",
    "PublicationPackage",
    "PersistentV5Store",
    "DiscoveryRun",
    "GenerationJob",
    "ProviderPreflight",
    "ProviderStatus",
    "format_preflight_report",
    "CostLedger",
    "CostEntry",
    "CostStatus",
    "GenerationWorker",
]

_LAZY: dict[str, tuple[str, str]] = {
    "RunStoryAdapter": (".run_story_adapter", "RunStoryAdapter"),
    "VersionStore": (".version_store", "VersionStore"),
    "ReviewSystem": (".review_system", "ReviewSystem"),
    "PublicationPackage": (".publication_package", "PublicationPackage"),
    "PersistentV5Store": (".persistent_store", "PersistentV5Store"),
    "DiscoveryRun": (".persistent_store", "DiscoveryRun"),
    "GenerationJob": (".persistent_store", "GenerationJob"),
    "ProviderPreflight": (".provider_preflight", "ProviderPreflight"),
    "ProviderStatus": (".provider_preflight", "ProviderStatus"),
    "format_preflight_report": (".provider_preflight", "format_preflight_report"),
    "CostLedger": (".cost_ledger", "CostLedger"),
    "CostEntry": (".cost_ledger", "CostEntry"),
    "CostStatus": (".cost_ledger", "CostStatus"),
    "GenerationWorker": (".generation_worker", "GenerationWorker"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr = target
    from importlib import import_module

    mod = import_module(module_name, __name__)
    value = getattr(mod, attr)
    globals()[name] = value
    return value
