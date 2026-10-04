"""V6 deep research: free multi-source retrieval for one news event.

Pipeline: cluster seeds + news search (Bing/Google News RSS) -> HTTP fetch
(headless Edge fallback) -> main-content extraction -> chrome stripping ->
relevance + syndication filtering -> primary-source link following.

Output is a ``ResearchDossier`` of clean source documents. Raw text here is
research material for fact extraction; it is never handed to the writer as prose.
"""

from newsagent_v2.research.dossier import ResearchDossier, SourceDoc
from newsagent_v2.research.pipeline import ResearchConfig, deep_research

__all__ = ["ResearchConfig", "ResearchDossier", "SourceDoc", "deep_research"]
