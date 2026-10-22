"""Per-sport RAG source filters for search_knowledge_base.

Maps prompt_mode → corpus path prefixes / globs under knowledge/corpus/.
When the model omits sources=, agent.py applies these defaults.
"""

from __future__ import annotations

# prompt_mode → filter patterns (directory prefixes and/or globs)
SPORT_KB_SOURCES: dict[str, list[str]] = {
    "general": ["human/", "opensim_motion_tools.txt"],
    "golf": ["golf/", "golf_*.txt"],
    "snowboarding": ["fis/", "snowboarding/", "snowboard_*.txt"],
    "baseball": ["baseball/"],
    "tennis": ["tennis/"],
    "basketball": ["basketball/"],
    "soccer": ["soccer/"],
    "running": ["running/"],
    "weightlifting": ["weightlifting/"],
    "swimming": ["swimming/"],
    "volleyball": ["volleyball/"],
    "boxing": ["boxing/"],
    "yoga": ["yoga/"],
}


def default_kb_sources(prompt_mode: str | None) -> list[str] | None:
    if not prompt_mode:
        return None
    key = prompt_mode.strip().lower()
    src = SPORT_KB_SOURCES.get(key)
    return list(src) if src else None


def known_prompt_modes() -> list[str]:
    return sorted(SPORT_KB_SOURCES.keys())
