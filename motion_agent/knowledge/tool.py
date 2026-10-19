"""Knowledge-base tools for the motion agent (local RAG)."""

from __future__ import annotations

from typing import Any

from motion_agent.knowledge import (
    DEFAULT_TOP_K,
    get_index,
    knowledge_status,
    search_chunks,
)
from motion_agent.motion.sequence import MotionSequence
from motion_agent.storage.errors import tool_error


def search_knowledge_base(
    seq: MotionSequence,
    query: str,
    top_k: int | None = None,
    min_score: float = 0.15,
    sources: list[str] | None = None,
) -> dict[str, Any]:
    """Retrieve top-k text chunks from the local corpus (not uploaded to Qwen)."""
    del seq  # session-independent
    q = (query or "").strip()
    if not q:
        return tool_error("INVALID_ARGUMENT", "query is required")
    k = int(top_k) if top_k is not None else DEFAULT_TOP_K
    k = max(1, min(k, 12))
    try:
        hits = search_chunks(
            q,
            top_k=k,
            min_score=float(min_score),
            sources=sources,
        )
    except Exception as exc:  # noqa: BLE001
        return tool_error("KB_SEARCH_FAILED", f"{type(exc).__name__}: {exc}")

    status = knowledge_status()
    return {
        "error": False,
        "query": q,
        "top_k": k,
        "min_score": float(min_score),
        "hit_count": len(hits),
        "hits": hits,
        "corpus_dir": status.get("corpus_dir"),
        "chunk_count": status.get("chunk_count"),
        "note": (
            "Local RAG retrieval only — whole files are NOT uploaded to the model. "
            "Use cited hit text as evidence; prefer motion tools for numeric kinematics. "
            "sources= accepts directory prefixes (golf/, fis/) and globs (golf_*.txt)."
        ),
    }


def list_knowledge_sources(seq: MotionSequence) -> dict[str, Any]:
    del seq
    try:
        idx = get_index()
        status = knowledge_status()
    except Exception as exc:  # noqa: BLE001
        return tool_error("KB_STATUS_FAILED", f"{type(exc).__name__}: {exc}")
    by_source: dict[str, int] = {}
    for c in idx.chunks:
        by_source[c.source] = by_source.get(c.source, 0) + 1
    return {
        "error": False,
        "corpus_dir": status.get("corpus_dir"),
        "index_dir": status.get("index_dir"),
        "embed_model": status.get("embed_model"),
        "file_count": status.get("file_count"),
        "chunk_count": status.get("chunk_count"),
        "sources": [
            {"source": s, "chunks": n} for s, n in sorted(by_source.items())
        ],
        "note": "Call search_knowledge_base(query=...) to retrieve relevant passages.",
    }
