# Volleyball Sport Pack Knowledge Base

Status: content build v1 (2026-10-21)
Product: CeRebel body-first coaching (no ball tracking required)
Spec: corpus/_templates/SPORT_PACK_KB_SPEC.txt

## Scope (v1)

Primary motion families:
- **Attack approach** (3-step and 4-step variants): ready → approach → plant → takeoff → airborne swing → contact proxy → landing.
- **Jump serve** (subset): toss/load → approach or in-place load → takeoff → strike → landing (shares phase IDs where possible).

Secondary glossary (no dedicated v1 detectors):
- Block jump, setter footwork, forearm pass platform — concepts in Taxonomy; findings focus on approach/spike chain.

Out of scope v1:
- Ball trajectory, set height, net touch, rotation calls, court positioning without body inference.
- Medical diagnosis, return-to-play after shoulder/knee injury (redirect to licensed providers).

## Stable IDs

Phase IDs: ready, approach, plant, takeoff, swing, contact, landing
Finding slugs: see Findings/001_common_faults.txt (prefix optional: fault_ in taxonomy only)

## Indexing

All paths except `_meta/` are RAG-indexed. `_meta/build_report.txt` is ops-only.

## Versioning

Breaking changes to phase IDs or finding slugs require a migration note here and in Docs/001_ARCHITECTURE.txt.
