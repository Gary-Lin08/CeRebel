"""Local RAG knowledge base for the motion agent.

Text files stay on disk. Only top-k retrieved chunks are returned to the VLM —
nothing uploads whole documents into Qwen's context.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

LOG = logging.getLogger("motion_agent.knowledge")

DEFAULT_CORPUS_DIR = Path(__file__).resolve().parent / "corpus"
DEFAULT_INDEX_DIR = Path(
    os.environ.get(
        "MOTION_AGENT_KB_INDEX_DIR",
        str(Path(__file__).resolve().parents[1] / ".cache" / "kb_index"),
    )
)
DEFAULT_EMBED_MODEL = os.environ.get(
    "MOTION_AGENT_KB_EMBED_MODEL", "text-embedding-v3"
)
DEFAULT_CHUNK_CHARS = int(os.environ.get("MOTION_AGENT_KB_CHUNK_CHARS", "900"))
DEFAULT_CHUNK_OVERLAP = int(os.environ.get("MOTION_AGENT_KB_CHUNK_OVERLAP", "120"))
DEFAULT_TOP_K = int(os.environ.get("MOTION_AGENT_KB_TOP_K", "4"))


@dataclass
class Chunk:
    chunk_id: str
    source: str  # relative path within corpus
    text: str
    start_char: int
    end_char: int


class Embedder(Protocol):
    def embed(self, texts: list[str], *, text_type: str = "document") -> np.ndarray:
        """Return (N, D) float32 vectors."""


def corpus_dir() -> Path:
    raw = os.environ.get("MOTION_AGENT_KB_CORPUS_DIR")
    return Path(raw).expanduser().resolve() if raw else DEFAULT_CORPUS_DIR


def index_dir() -> Path:
    return Path(
        os.environ.get("MOTION_AGENT_KB_INDEX_DIR", str(DEFAULT_INDEX_DIR))
    ).expanduser().resolve()


def list_txt_files(root: Path | None = None) -> list[Path]:
    root = root or corpus_dir()
    if not root.is_dir():
        return []
    files: list[Path] = []
    for p in root.rglob("*.txt"):
        if not p.is_file():
            continue
        # Skip pack-internal manifests / build notes under */_meta/*
        if any(part == "_meta" for part in p.relative_to(root).parts):
            continue
        files.append(p)
    return sorted(files)


def chunk_text(
    text: str,
    *,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[tuple[int, int, str]]:
    """Split on paragraph boundaries when possible; fall back to sliding windows."""
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []
    if len(text) <= chunk_chars:
        return [(0, len(text), text)]

    paras = re.split(r"\n\s*\n", text)
    chunks: list[tuple[int, int, str]] = []
    buf = ""
    buf_start = 0
    cursor = 0

    def flush() -> None:
        nonlocal buf, buf_start
        s = buf.strip()
        if s:
            # locate s inside original near buf_start
            idx = text.find(s, max(0, buf_start - 5))
            if idx < 0:
                idx = buf_start
            chunks.append((idx, idx + len(s), s))
        buf = ""

    for para in paras:
        para = para.strip()
        if not para:
            cursor += 2
            continue
        pos = text.find(para, cursor)
        if pos < 0:
            pos = cursor
        if not buf:
            if len(para) <= chunk_chars:
                buf_start = pos
                buf = para
            else:
                start = 0
                while start < len(para):
                    end = min(len(para), start + chunk_chars)
                    piece = para[start:end].strip()
                    if piece:
                        abs_start = pos + start
                        chunks.append((abs_start, abs_start + len(piece), piece))
                    if end >= len(para):
                        break
                    start = max(end - overlap, start + 1)
                buf = ""
        elif len(buf) + 2 + len(para) <= chunk_chars:
            buf = buf + "\n\n" + para
        else:
            flush()
            if len(para) <= chunk_chars:
                buf_start = pos
                buf = para
            else:
                # hard-split long paragraph
                start = 0
                while start < len(para):
                    end = min(len(para), start + chunk_chars)
                    piece = para[start:end].strip()
                    if piece:
                        abs_start = pos + start
                        chunks.append((abs_start, abs_start + len(piece), piece))
                    if end >= len(para):
                        break
                    start = max(end - overlap, start + 1)
                buf = ""
        cursor = pos + len(para)

    flush()
    # Safety: never emit a chunk over the embedder limit.
    max_len = min(chunk_chars * 2, 7500)
    fixed: list[tuple[int, int, str]] = []
    for start, end, piece in (chunks or [(0, len(text), text)]):
        if len(piece) <= max_len:
            fixed.append((start, end, piece))
            continue
        s0 = 0
        while s0 < len(piece):
            s1 = min(len(piece), s0 + chunk_chars)
            sub = piece[s0:s1].strip()
            if sub:
                fixed.append((start + s0, start + s0 + len(sub), sub))
            if s1 >= len(piece):
                break
            s0 = max(s1 - overlap, s0 + 1)
    return fixed


def load_corpus_chunks(
    root: Path | None = None,
    *,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[Chunk]:
    root = (root or corpus_dir()).resolve()
    out: list[Chunk] = []
    for path in list_txt_files(root):
        rel = str(path.relative_to(root)).replace("\\", "/")
        raw = path.read_text(encoding="utf-8", errors="replace")
        for i, (start, end, piece) in enumerate(
            chunk_text(raw, chunk_chars=chunk_chars, overlap=overlap)
        ):
            digest = hashlib.sha1(f"{rel}:{start}:{end}:{piece}".encode()).hexdigest()[:12]
            out.append(
                Chunk(
                    chunk_id=f"{rel}#{i}-{digest}",
                    source=rel,
                    text=piece,
                    start_char=start,
                    end_char=end,
                )
            )
    return out


def corpus_fingerprint(root: Path | None = None) -> str:
    root = (root or corpus_dir()).resolve()
    h = hashlib.sha1()
    for path in list_txt_files(root):
        rel = str(path.relative_to(root)).replace("\\", "/")
        st = path.stat()
        h.update(rel.encode())
        h.update(str(st.st_mtime_ns).encode())
        h.update(str(st.st_size).encode())
    h.update(str(DEFAULT_CHUNK_CHARS).encode())
    h.update(str(DEFAULT_CHUNK_OVERLAP).encode())
    h.update(DEFAULT_EMBED_MODEL.encode())
    return h.hexdigest()


class HashingEmbedder:
    """Deterministic offline embedder for unit tests (no API).

    Mostly bag-of-words hashing so related queries retrieve matching docs.
    """

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim
        self.model = "HashingEmbedder"

    def embed(self, texts: list[str], *, text_type: str = "document") -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            v = np.zeros(self.dim, dtype=np.float32)
            toks = re.findall(r"[a-z0-9_]+", (t or "").lower())
            if text_type == "query":
                # light boost for query tokens
                weight = 1.25
            else:
                weight = 1.0
            for tok in toks:
                v[hash(tok) % self.dim] += weight
                if len(tok) > 4:
                    v[hash(tok[:-1]) % self.dim] += 0.35 * weight
            n = float(np.linalg.norm(v)) or 1.0
            out[i] = v / n
        return out


class DashScopeEmbedder:
    """OpenAI-compatible embeddings via DashScope."""

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.model = model or DEFAULT_EMBED_MODEL
        self.base_url = (
            base_url
            or os.environ.get("MOTION_AGENT_KB_EMBED_BASE_URL")
            or os.environ.get("MOTION_AGENT_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        )
        self.api_key = api_key or (os.environ.get("MOTION_AGENT_KB_EMBED_API_KEY") or "").strip() or None
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI

            from motion_agent.llm.qwen_client import _load_api_key

            self._client = OpenAI(
                api_key=self.api_key or _load_api_key(),
                base_url=self.base_url,
                timeout=60.0,
            )
        return self._client

    def embed(self, texts: list[str], *, text_type: str = "document") -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        # DashScope embedding input length cap is 8192 characters.
        cleaned = [(t or "").strip()[:8000] for t in texts]
        cleaned = [t if t else " " for t in cleaned]
        client = self._get_client()
        try:
            resp = client.embeddings.create(
                model=self.model,
                input=cleaned,
                extra_body={"text_type": text_type},
            )
        except Exception:
            resp = client.embeddings.create(model=self.model, input=cleaned)
        rows = sorted(resp.data, key=lambda d: d.index)
        arr = np.array([d.embedding for d in rows], dtype=np.float32)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms = np.maximum(norms, 1e-8)
        return arr / norms


@dataclass
class KnowledgeIndex:
    fingerprint: str
    embed_model: str
    chunks: list[Chunk]
    embeddings: np.ndarray  # (N, D)

    def save(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        meta = {
            "fingerprint": self.fingerprint,
            "embed_model": self.embed_model,
            "chunks": [asdict(c) for c in self.chunks],
        }
        (directory / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        np.save(directory / "embeddings.npy", self.embeddings)

    @classmethod
    def load(cls, directory: Path) -> KnowledgeIndex | None:
        meta_path = directory / "meta.json"
        emb_path = directory / "embeddings.npy"
        if not meta_path.is_file() or not emb_path.is_file():
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        chunks = [Chunk(**c) for c in meta.get("chunks") or []]
        emb = np.load(emb_path)
        if emb.shape[0] != len(chunks):
            return None
        return cls(
            fingerprint=str(meta.get("fingerprint") or ""),
            embed_model=str(meta.get("embed_model") or ""),
            chunks=chunks,
            embeddings=emb.astype(np.float32),
        )


def build_index(
    root: Path | None = None,
    *,
    embedder: Embedder | None = None,
    batch_size: int = 10,
) -> KnowledgeIndex:
    root = (root or corpus_dir()).resolve()
    chunks = load_corpus_chunks(root)
    embedder = embedder or DashScopeEmbedder()
    if not chunks:
        LOG.warning("knowledge corpus empty at %s", root)
        return KnowledgeIndex(
            fingerprint=corpus_fingerprint(root),
            embed_model=getattr(embedder, "model", "none"),
            chunks=[],
            embeddings=np.zeros((0, 1), dtype=np.float32),
        )

    vectors: list[np.ndarray] = []
    for i in range(0, len(chunks), batch_size):
        batch = [c.text for c in chunks[i : i + batch_size]]
        vectors.append(embedder.embed(batch, text_type="document"))
    emb = np.vstack(vectors).astype(np.float32)
    idx = KnowledgeIndex(
        fingerprint=corpus_fingerprint(root),
        embed_model=str(getattr(embedder, "model", type(embedder).__name__)),
        chunks=chunks,
        embeddings=emb,
    )
    LOG.info(
        "built knowledge index corpus=%s chunks=%s dim=%s model=%s",
        root,
        len(chunks),
        emb.shape[1] if emb.size else 0,
        idx.embed_model,
    )
    return idx


_INDEX: KnowledgeIndex | None = None


def get_index(
    *,
    force_rebuild: bool = False,
    embedder: Embedder | None = None,
    persist: bool = True,
) -> KnowledgeIndex:
    global _INDEX
    root = corpus_dir()
    dest = index_dir()
    fp = corpus_fingerprint(root)

    if not force_rebuild and _INDEX is not None and _INDEX.fingerprint == fp:
        return _INDEX

    if not force_rebuild and embedder is None:
        loaded = KnowledgeIndex.load(dest)
        if loaded is not None and loaded.fingerprint == fp:
            _INDEX = loaded
            return loaded

    idx = build_index(root, embedder=embedder)
    if persist:
        try:
            idx.save(dest)
        except Exception:  # noqa: BLE001
            LOG.exception("failed to persist knowledge index to %s", dest)
    _INDEX = idx
    return idx


def reset_index_cache() -> None:
    global _INDEX
    _INDEX = None


def source_matches(chunk_source: str, patterns: list[str] | set[str] | None) -> bool:
    """True if chunk_source matches any filter pattern.

    Patterns support:
      - exact corpus-relative paths
      - directory prefixes (``golf/``, ``golf/Rules``, ``fis``)
      - globs (``golf_*.txt``, ``**/Equipment/*.txt``)
    """
    if not patterns:
        return True
    src = chunk_source.replace("\\", "/")
    base = Path(src).name
    for raw in patterns:
        pat = (raw or "").replace("\\", "/").strip()
        if not pat:
            continue
        if any(ch in pat for ch in "*?[]"):
            if fnmatch.fnmatch(src, pat) or fnmatch.fnmatch(base, pat):
                return True
            continue
        if src == pat:
            return True
        # Directory / path prefix: "golf/" or "golf" or "golf/Rules"
        prefix = pat if pat.endswith("/") else pat + "/"
        if src.startswith(prefix):
            return True
    return False


def search_chunks(
    query: str,
    *,
    top_k: int | None = None,
    min_score: float = 0.0,
    sources: list[str] | None = None,
    embedder: Embedder | None = None,
    index: KnowledgeIndex | None = None,
) -> list[dict[str, Any]]:
    q = (query or "").strip()
    if not q:
        return []
    top_k = top_k or DEFAULT_TOP_K
    idx = index or get_index(embedder=embedder)
    if not idx.chunks:
        return []

    query_embedder = embedder
    if query_embedder is None:
        if str(idx.embed_model).startswith("Hashing"):
            query_embedder = HashingEmbedder(dim=int(idx.embeddings.shape[1] or 64))
        else:
            query_embedder = DashScopeEmbedder(model=idx.embed_model)

    qv = query_embedder.embed([q], text_type="query")[0]
    if qv.shape[0] != idx.embeddings.shape[1]:
        raise ValueError(
            f"Query embedding dim {qv.shape[0]} != index dim {idx.embeddings.shape[1]}"
        )
    scores = idx.embeddings @ qv
    order = np.argsort(-scores)
    hits: list[dict[str, Any]] = []
    patterns = [s for s in (sources or []) if s] or None
    for i in order:
        chunk = idx.chunks[int(i)]
        if patterns is not None and not source_matches(chunk.source, patterns):
            continue
        score = float(scores[int(i)])
        if score < min_score:
            continue
        hits.append(
            {
                "chunk_id": chunk.chunk_id,
                "source": chunk.source,
                "score": round(score, 4),
                "text": chunk.text,
                "start_char": chunk.start_char,
                "end_char": chunk.end_char,
            }
        )
        if len(hits) >= top_k:
            break
    return hits


def knowledge_status() -> dict[str, Any]:
    root = corpus_dir()
    files = list_txt_files(root)
    idx = None
    try:
        idx = get_index(persist=True)
    except Exception as exc:  # noqa: BLE001
        return {
            "error": True,
            "message": str(exc),
            "corpus_dir": str(root),
            "file_count": len(files),
            "files": [str(p.relative_to(root)) for p in files],
        }
    return {
        "error": False,
        "corpus_dir": str(root),
        "index_dir": str(index_dir()),
        "file_count": len(files),
        "files": [str(p.relative_to(root)) for p in files],
        "chunk_count": len(idx.chunks) if idx else 0,
        "embed_model": idx.embed_model if idx else None,
        "fingerprint": idx.fingerprint if idx else None,
    }
