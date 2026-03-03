"""OpenViking Curator — Knowledge governance layer."""

from ._version import __version__
from .backend import KnowledgeBackend, SearchResponse, SearchResult
from .backend_memory import InMemoryBackend
from .backend_ov import OpenVikingBackend
from .config import chat, env, log, validate_config
from .pipeline_v2 import run
from .review import JudgeResult

__all__ = [
    "run",
    "KnowledgeBackend",
    "SearchResult",
    "SearchResponse",
    "OpenVikingBackend",
    "InMemoryBackend",
    "JudgeResult",
    "chat",
    "env",
    "log",
    "validate_config",
    "__version__",
]
