#!/usr/bin/env python3
"""Rebuild the local knowledge-base vector index from MOTION_AGENT_KB_CORPUS_DIR."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hashing",
        action="store_true",
        help="Use offline HashingEmbedder (no API) for smoke tests",
    )
    parser.add_argument(
        "--corpus",
        type=str,
        default=None,
        help="Override corpus directory (sets MOTION_AGENT_KB_CORPUS_DIR)",
    )
    parser.add_argument(
        "--index-dir",
        type=str,
        default=None,
        help="Override index directory (sets MOTION_AGENT_KB_INDEX_DIR)",
    )
    args = parser.parse_args(argv)

    if args.corpus:
        os.environ["MOTION_AGENT_KB_CORPUS_DIR"] = args.corpus
    if args.index_dir:
        os.environ["MOTION_AGENT_KB_INDEX_DIR"] = args.index_dir

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    from motion_agent.knowledge import (
        HashingEmbedder,
        get_index,
        knowledge_status,
        reset_index_cache,
    )

    reset_index_cache()
    embedder = HashingEmbedder() if args.hashing else None
    idx = get_index(force_rebuild=True, embedder=embedder, persist=True)
    status = knowledge_status()
    print(
        json.dumps(
            {
                "chunks": len(idx.chunks),
                "embed_model": idx.embed_model,
                "fingerprint": idx.fingerprint,
                "status": status,
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
