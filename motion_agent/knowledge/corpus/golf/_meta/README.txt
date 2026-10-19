Golf knowledge corpus (FIS-style layout)
========================================

Source zip: golf_kb_primary_source_pack(1).zip
Ingested from: /tmp/golf_kb_ingest/golf_kb_primary_source_pack
Converted documents: 14

Category counts:
  Biomechanics: 2
  Docs: 2
  Equipment: 2
  Handicap: 1
  Rules: 4
  Schema: 1
  Taxonomy: 2

Layout mirrors FIS snowboarding packs under knowledge/corpus/fis/:
  Rules/          R&A / USGA Rules of Golf, clarifications, local rules
  Handicap/       World Handicap System / Rules of Handicapping
  Equipment/      Equipment Rules + conforming ball list
  Biomechanics/   Open-access swing / GRF–CoP systematic reviews
  Taxonomy/       Bilingual concept + relation CSVs
  Schema/         Golf ontology (JSON-LD)
  Docs/           Architecture + ecosystem research
  _meta/          Manifests, download/build reports, this README

Seed technique packs (sibling of this folder, also indexed):
  knowledge/corpus/golf_swing_vocab.txt
  knowledge/corpus/golf_biomechanics_conventions.txt

Rebuild embeddings after ingest:
  PYTHONPATH=/root/CerebelServer \
    python3 motion_agent/scripts/rebuild_knowledge_index.py

Converted files:
  Rules/001_2023_Rules_of_Golf.txt  (410195 chars)
  Rules/002_2026-07-01_Additional_Clarifications.txt  (31347 chars)
  Rules/003_USGA_15_Rules_Poster_2023.txt  (4686 chars)
  Rules/004_USGA_Local_Rules_and_Terms_2023.txt  (4848 chars)
  Handicap/001_2024_Rules_of_Handicapping_USGA.txt  (132820 chars)
  Equipment/001_Conforming_Golf_Balls_CURRENT.txt  (223697 chars)
  Equipment/002_Equipment_Rules.txt  (169642 chars)
  Biomechanics/001_GRF_CoP_Golf_Systematic_Review_2026.txt  (73058 chars)
  Biomechanics/002_Golf_Swing_Biomechanics_Systematic_Review_2022.txt  (111477 chars)
  Taxonomy/001_concepts.txt  (4101 chars)
  Taxonomy/002_relations.txt  (832 chars)
  Schema/001_golf-ontology.txt  (3637 chars)
  Docs/001_ARCHITECTURE.txt  (1581 chars)
  Docs/002_ecosystem_research_zh.txt  (20925 chars)
