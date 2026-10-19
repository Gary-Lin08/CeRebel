"""Unit tests for local RAG knowledge base (offline hashing embedder)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from motion_agent.knowledge import (
    HashingEmbedder,
    build_index,
    reset_index_cache,
    search_chunks,
)
from motion_agent.knowledge.tool import list_knowledge_sources, search_knowledge_base
from motion_agent.llm.schemas import openai_tools
from motion_agent.tools.motion_tools import build_tool_map


class TestKnowledgeRAG(unittest.TestCase):
    def setUp(self) -> None:
        reset_index_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.corpus = Path(self.tmp.name) / "corpus"
        self.index_dir = Path(self.tmp.name) / "index"
        self.corpus.mkdir()
        (self.corpus / "alpha.txt").write_text(
            "The ALPHA protocol says backflip uses positive intrinsic somersault.\n\n"
            "Never upload entire manuals into the model context; retrieve passages instead.\n",
            encoding="utf-8",
        )
        (self.corpus / "beta.txt").write_text(
            "The BETA handbook covers estimated GRF uncertainty in OpenSim dynamics.\n",
            encoding="utf-8",
        )
        self.embedder = HashingEmbedder(dim=256)

    def tearDown(self) -> None:
        reset_index_cache()
        self.tmp.cleanup()

    def test_build_and_search(self) -> None:
        idx = build_index(self.corpus, embedder=self.embedder)
        self.assertGreaterEqual(len(idx.chunks), 2)
        hits = search_chunks(
            "positive intrinsic somersault backflip",
            top_k=2,
            min_score=0.0,
            embedder=self.embedder,
            index=idx,
        )
        self.assertTrue(hits)
        self.assertIn("backflip", hits[0]["text"].lower())
        self.assertEqual(hits[0]["source"], "alpha.txt")

    def test_tool_wrappers(self) -> None:
        idx = build_index(self.corpus, embedder=self.embedder)
        # Monkeypatch get_index via search_chunks path by calling tool with injected index
        # through search_knowledge_base → search_chunks; temporarily patch module.
        import motion_agent.knowledge.tool as tool_mod
        import motion_agent.knowledge as kb_mod

        def _fake_search(query, **kwargs):
            return search_chunks(
                query,
                top_k=kwargs.get("top_k") or 3,
                min_score=float(kwargs.get("min_score") or 0.0),
                sources=kwargs.get("sources"),
                embedder=self.embedder,
                index=idx,
            )

        def _fake_status():
            return {
                "corpus_dir": str(self.corpus),
                "chunk_count": len(idx.chunks),
            }

        orig_search = tool_mod.search_chunks
        orig_status = tool_mod.knowledge_status
        orig_get = tool_mod.get_index
        tool_mod.search_chunks = _fake_search  # type: ignore[assignment]
        tool_mod.knowledge_status = _fake_status  # type: ignore[assignment]
        tool_mod.get_index = lambda: idx  # type: ignore[assignment]
        try:
            out = search_knowledge_base(None, query="backflip somersault", top_k=2)  # type: ignore[arg-type]
            self.assertFalse(out.get("error"), out)
            self.assertGreaterEqual(out["hit_count"], 1)
            listed = list_knowledge_sources(None)  # type: ignore[arg-type]
            self.assertFalse(listed.get("error"), listed)
            names = {s["source"] for s in listed["sources"]}
            self.assertEqual(names, {"alpha.txt", "beta.txt"})
        finally:
            tool_mod.search_chunks = orig_search  # type: ignore[assignment]
            tool_mod.knowledge_status = orig_status  # type: ignore[assignment]
            tool_mod.get_index = orig_get  # type: ignore[assignment]
            del kb_mod

    def test_tools_registered(self) -> None:
        names = {t["function"]["name"] for t in openai_tools()}
        self.assertIn("search_knowledge_base", names)
        self.assertIn("list_knowledge_sources", names)
        # build_tool_map needs a seq only as a handle; use a simple namespace
        class _Seq:
            pass

        tools = build_tool_map(_Seq())  # type: ignore[arg-type]
        self.assertIn("search_knowledge_base", tools)

    def test_sources_prefix_and_glob(self) -> None:
        from motion_agent.knowledge import source_matches

        self.assertTrue(source_matches("golf/Rules/a.txt", ["golf/"]))
        self.assertTrue(source_matches("golf/Rules/a.txt", ["golf"]))
        self.assertTrue(source_matches("golf/Rules/a.txt", ["golf/Rules/"]))
        self.assertFalse(source_matches("fis/Rules/a.txt", ["golf/"]))
        self.assertTrue(source_matches("golf_swing_vocab.txt", ["golf_*.txt"]))
        self.assertFalse(source_matches("snowboard_aerial_vocab.txt", ["golf_*.txt"]))

        (self.corpus / "golf").mkdir()
        (self.corpus / "golf" / "rules.txt").write_text(
            "Penalty area relief under the Rules of Golf requires dropping.\n",
            encoding="utf-8",
        )
        (self.corpus / "fis_noise.txt").write_text(
            "Snowboard halfpipe judging criteria and FIS points.\n",
            encoding="utf-8",
        )
        idx = build_index(self.corpus, embedder=self.embedder)
        hits = search_chunks(
            "penalty area relief Rules of Golf",
            top_k=3,
            min_score=0.0,
            sources=["golf/"],
            embedder=self.embedder,
            index=idx,
        )
        self.assertTrue(hits)
        self.assertTrue(all(h["source"].startswith("golf/") for h in hits))

    def test_list_txt_files_skips_meta(self) -> None:
        from motion_agent.knowledge import list_txt_files

        meta = self.corpus / "golf" / "_meta"
        meta.mkdir(parents=True)
        (meta / "README.txt").write_text("manifest only\n", encoding="utf-8")
        (self.corpus / "golf").mkdir(exist_ok=True)
        (self.corpus / "golf" / "keep.txt").write_text("keep me\n", encoding="utf-8")
        paths = list_txt_files(self.corpus)
        rels = [str(p.relative_to(self.corpus)) for p in paths]
        self.assertTrue(any(r.endswith("keep.txt") for r in rels))
        self.assertFalse(any("_meta" in r for r in rels))


if __name__ == "__main__":
    unittest.main()
