#!/usr/bin/env python3
"""Ingest a documents zip into motion_agent/knowledge/corpus/<dest>/ as .txt.

Defaults target the FIS snowboarding tree for back-compat. For golf (or any
other sport pack), pass --out pointing at e.g. knowledge/corpus/golf.

Recognizes:
  - FIS-style zips with a documents/ subtree and category folders
  - Golf primary-source packs (01_rules/, 02_handicap/, …) nested under a
    single top-level directory; numbered folders are mapped to FIS-like names
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path


# Noise / tooling dirs never converted into the RAG corpus.
SKIP_DIR_NAMES = {
    "__MACOSX",
    "__pycache__",
    ".git",
    "tmp",
    "tests",
    "08_tools",
    "generated_corpus",
    "node_modules",
}

# Golf pack numbered folders → FIS-style category names under corpus/golf/.
GOLF_CATEGORY_MAP: dict[str, str] = {
    "01_rules": "Rules",
    "02_handicap": "Handicap",
    "03_equipment": "Equipment",
    "04_biomechanics_open_access": "Biomechanics",
    "05_taxonomy": "Taxonomy",
    "06_schema": "Schema",
    "07_docs": "Docs",
}

TEXTISH_SUFFIXES = {
    ".pdf",
    ".docx",
    ".txt",
    ".csv",
    ".md",
    ".json",
    ".jsonld",
    ".jsonl",
}


def _safe_stem(name: str) -> str:
    stem = Path(name).stem
    stem = re.sub(r"[^\w.\-]+", "_", stem, flags=re.UNICODE)
    return stem.strip("._") or "doc"


def _category_dir(rel_parent: Path) -> Path:
    """Map golf numbered category folders to FIS-like names; else keep path."""
    if not rel_parent.parts:
        return Path()
    first = rel_parent.parts[0]
    mapped = GOLF_CATEGORY_MAP.get(first)
    if mapped is None:
        return rel_parent
    rest = Path(*rel_parent.parts[1:]) if len(rel_parent.parts) > 1 else Path()
    return Path(mapped) / rest if rest != Path() else Path(mapped)


def pdf_to_text(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    parts: list[str] = []
    for i, page in enumerate(reader.pages):
        try:
            t = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            t = f"[page {i + 1} extract error: {exc}]"
        t = t.strip()
        if t:
            parts.append(f"--- page {i + 1} ---\n{t}")
    return "\n\n".join(parts).strip()


def docx_to_text(path: Path) -> str:
    try:
        from docx import Document  # type: ignore
    except ImportError as exc:
        raise RuntimeError("python-docx required for .docx") from exc
    doc = Document(str(path))
    paras = [p.text.strip() for p in doc.paragraphs if p.text and p.text.strip()]
    return "\n\n".join(paras).strip()


def convert_one(src: Path) -> str:
    suf = src.suffix.lower()
    if suf == ".pdf":
        return pdf_to_text(src)
    if suf == ".docx":
        return docx_to_text(src)
    if suf in {".txt", ".csv", ".md", ".json", ".jsonld", ".jsonl"}:
        return src.read_text(encoding="utf-8", errors="replace").strip()
    if suf in {".doc", ".xlsx", ".xlsm"}:
        return ""  # binary / legacy — skip for RAG text
    return ""


def _find_docs_root(extract_root: Path) -> Path:
    """Prefer documents/; else nested single pack dir; else extract root."""
    docs = extract_root / "documents"
    if docs.is_dir():
        return docs
    # Zip may wrap everything in one top-level folder (golf pack / macOS).
    children = [
        p
        for p in extract_root.iterdir()
        if p.is_dir() and p.name not in SKIP_DIR_NAMES and not p.name.startswith(".")
    ]
    if len(children) == 1:
        nested = children[0]
        if (nested / "documents").is_dir():
            return nested / "documents"
        return nested
    return extract_root


def _should_skip_path(path: Path, docs_root: Path) -> bool:
    try:
        rel_parts = path.relative_to(docs_root).parts
    except ValueError:
        rel_parts = path.parts
    if any(part in SKIP_DIR_NAMES for part in rel_parts):
        return True
    if path.name.startswith("._") or path.name == ".DS_Store":
        return True
    return False


def _write_meta_readme(
    out_root: Path,
    *,
    zip_path: Path,
    docs_root: Path,
    written_files: list[tuple[str, int]],
    category_counts: dict[str, int],
) -> None:
    meta = out_root / "_meta"
    meta.mkdir(parents=True, exist_ok=True)

    # Prefer pack README if present.
    for candidate in (
        docs_root / "README.md",
        docs_root.parent / "README.md",
        docs_root / "RUN_ME_FIRST.txt",
    ):
        if candidate.is_file():
            text = convert_one(candidate)
            if text:
                (meta / "pack_readme.txt").write_text(text + "\n", encoding="utf-8")
            break

    # Manifest / download reports → _meta
    for name in (
        "SOURCE_MANIFEST.csv",
        "download_manifest.csv",
        "README.txt",
    ):
        for base in (docs_root, docs_root.parent, docs_root / "08_tools"):
            src = base / name
            if src.is_file():
                text = convert_one(src)
                if text:
                    dest = meta / f"{_safe_stem(name)}.txt"
                    dest.write_text(text + "\n", encoding="utf-8")
                break

    for report_name in ("download_report.json", "download_report.txt"):
        src = docs_root / "08_tools" / report_name
        if src.is_file():
            text = convert_one(src)
            if text:
                (meta / f"{_safe_stem(report_name)}.txt").write_text(
                    text + "\n", encoding="utf-8"
                )

    build_report = docs_root / "generated_corpus" / "build_report.json"
    if build_report.is_file():
        text = convert_one(build_report)
        if text:
            (meta / "build_report.txt").write_text(text + "\n", encoding="utf-8")

    lines = [
        "Golf knowledge corpus (FIS-style layout)",
        "========================================",
        "",
        f"Source zip: {zip_path.name}",
        f"Ingested from: {docs_root}",
        f"Converted documents: {len(written_files)}",
        "",
        "Category counts:",
    ]
    for cat, n in sorted(category_counts.items()):
        lines.append(f"  {cat}: {n}")
    lines.extend(
        [
            "",
            "Layout mirrors FIS snowboarding packs under knowledge/corpus/fis/:",
            "  Rules/          R&A / USGA Rules of Golf, clarifications, local rules",
            "  Handicap/       World Handicap System / Rules of Handicapping",
            "  Equipment/      Equipment Rules + conforming ball list",
            "  Biomechanics/   Open-access swing / GRF–CoP systematic reviews",
            "  Taxonomy/       Bilingual concept + relation CSVs",
            "  Schema/         Golf ontology (JSON-LD)",
            "  Docs/           Architecture + ecosystem research",
            "  _meta/          Manifests, download/build reports, this README",
            "",
            "Seed technique packs (sibling of this folder, also indexed):",
            "  knowledge/corpus/golf_swing_vocab.txt",
            "  knowledge/corpus/golf_biomechanics_conventions.txt",
            "",
            "Rebuild embeddings after ingest:",
            "  PYTHONPATH=/root/CerebelServer \\",
            "    python3 motion_agent/scripts/rebuild_knowledge_index.py",
            "",
            "Converted files:",
        ]
    )
    for rel, nchars in written_files:
        lines.append(f"  {rel}  ({nchars} chars)")
    (meta / "README.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Compact ingest manifest CSV for auditing.
    manifest_path = meta / "ingest_manifest.txt"
    with manifest_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["relative_path", "chars"])
        for rel, nchars in written_files:
            w.writerow([rel, nchars])


def main(argv: list[str] | None = None) -> int:
    default_out = Path(__file__).resolve().parents[1] / "knowledge" / "corpus" / "fis"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "zip_path",
        type=Path,
        nargs="?",
        default=Path("/root/FIS_Snowboard_Freestyle_Freeski_Documents_2026-09-09.zip"),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Corpus output directory (default: knowledge/corpus/fis).",
    )
    parser.add_argument(
        "--corpus-fis",
        type=Path,
        default=None,
        help=argparse.SUPPRESS,  # back-compat alias for --out
    )
    parser.add_argument("--min-chars", type=int, default=80)
    parser.add_argument(
        "--extract-dir",
        type=Path,
        default=None,
        help="Temp extract directory (default: /tmp/<out.name>_kb_ingest).",
    )
    parser.add_argument(
        "--number-prefix",
        action="store_true",
        default=True,
        help="Prefix output filenames with 001_, 002_, … per category (default).",
    )
    parser.add_argument(
        "--no-number-prefix",
        action="store_false",
        dest="number_prefix",
        help="Keep original stems without numeric prefixes.",
    )
    args = parser.parse_args(argv)

    out_root: Path = (args.out or args.corpus_fis or default_out).expanduser().resolve()
    zip_path: Path = args.zip_path.expanduser().resolve()
    if not zip_path.is_file():
        print(f"missing zip: {zip_path}", file=sys.stderr)
        return 1

    extract_root = (
        args.extract_dir.expanduser().resolve()
        if args.extract_dir
        else Path(f"/tmp/{out_root.name}_kb_ingest")
    )
    if extract_root.exists():
        shutil.rmtree(extract_root)
    extract_root.mkdir(parents=True)

    print(f"extracting {zip_path} …", flush=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_root)

    docs_root = _find_docs_root(extract_root)
    print(f"docs_root={docs_root}", flush=True)

    if out_root.exists():
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True)

    written = 0
    skipped = 0
    empty = 0
    errors = 0
    written_files: list[tuple[str, int]] = []
    category_counts: dict[str, int] = {}
    # Per-category counters for numeric prefixes (FIS-style).
    cat_counters: dict[str, int] = {}

    for src in sorted(docs_root.rglob("*")):
        if not src.is_file():
            continue
        if _should_skip_path(src, docs_root):
            continue
        # Top-level pack meta is handled separately into _meta/.
        if src.parent == docs_root and src.name.lower() in {
            "readme.md",
            "readme.txt",
            "run_me_first.txt",
            "source_manifest.csv",
            "download_manifest.csv",
            "requirements.txt",
            ".gitignore",
            "download_all.command",
        }:
            continue
        suf = src.suffix.lower()
        if suf not in TEXTISH_SUFFIXES:
            skipped += 1
            continue
        # Skip pre-built RAG dumps; motion_agent chunks/embeds its own .txt.
        if src.name in {"chunks.jsonl", "golf_kb.sqlite"} or suf == ".sqlite":
            skipped += 1
            continue
        try:
            rel = src.relative_to(docs_root)
        except ValueError:
            rel = Path(src.name)
        cat_rel = _category_dir(rel.parent)
        cat_key = cat_rel.parts[0] if cat_rel.parts else "_root"
        cat_counters[cat_key] = cat_counters.get(cat_key, 0) + 1
        stem = _safe_stem(src.name)
        if args.number_prefix:
            stem = f"{cat_counters[cat_key]:03d}_{stem}"
        dest = out_root / cat_rel / f"{stem}.txt"
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            text = convert_one(src)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            print(f"ERR {rel}: {exc}", flush=True)
            continue
        if len(text) < args.min_chars:
            empty += 1
            # Roll back counter so gaps don't appear for failed docs.
            cat_counters[cat_key] -= 1
            print(f"EMPTY {rel} chars={len(text)}", flush=True)
            continue
        dest.write_text(text + "\n", encoding="utf-8")
        written += 1
        rel_out = str(dest.relative_to(out_root))
        written_files.append((rel_out, len(text)))
        category_counts[cat_key] = category_counts.get(cat_key, 0) + 1
        print(f"OK  {rel_out} chars={len(text)}", flush=True)

    _write_meta_readme(
        out_root,
        zip_path=zip_path,
        docs_root=docs_root,
        written_files=written_files,
        category_counts=category_counts,
    )
    # Count _meta files written by helper.
    meta_txts = list((out_root / "_meta").glob("*.txt"))
    print(
        f"meta wrote {len(meta_txts)} files under _meta/",
        flush=True,
    )

    print(
        f"done written={written} empty={empty} skipped_binary={skipped} "
        f"errors={errors} out={out_root}",
        flush=True,
    )
    print("categories: " + json.dumps(category_counts, sort_keys=True), flush=True)
    return 0 if written else 2


if __name__ == "__main__":
    raise SystemExit(main())
