"""Extract phase structure from a plan.md document.

The Architect writes phases as markdown headings like `## P1: Core gameplay` or
`### Phase 2 — Leaderboard`. We parse them out so the orchestrator knows which phase
to hand the Dispatcher, and so the UI can eventually show phase progress.

This is tolerant by design — plan format is human-writable markdown, not a rigid schema.
If the parser finds nothing, we fall back to treating the whole plan as a single phase
(so the system still works on weird plan shapes).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class ParsedPhase:
    id: str  # normalized, e.g. "P1"
    title: str  # e.g. "Core gameplay"


# Matches headings that introduce a phase. Variations we accept:
#   ## P1: Core gameplay
#   ## P1 — Core gameplay
#   ## P1 Core gameplay
#   ### Phase 1: Core gameplay
#   ## Phase 1 — Core gameplay
_PHASE_HEADING = re.compile(
    r"^\s*#{1,4}\s*"
    r"(?:"
    r"(?P<short>P(?P<num1>\d+))"  # "P1"
    r"|"
    r"Phase\s+(?P<num2>\d+)"  # "Phase 1"
    r")"
    r"\s*[:\u2014\u2013\-]?\s*"  # optional ":" / em-dash / en-dash / hyphen separator
    r"(?P<title>.*?)\s*$",
    re.IGNORECASE,
)


def parse_phases(plan_markdown: str) -> list[ParsedPhase]:
    """Extract phases from plan.md. Returns [] if none found."""
    if not plan_markdown:
        return []

    phases: list[ParsedPhase] = []
    seen_ids: set[str] = set()

    for line in plan_markdown.splitlines():
        m = _PHASE_HEADING.match(line)
        if not m:
            continue
        num = m.group("num1") or m.group("num2")
        if num is None:
            continue
        phase_id = f"P{int(num)}"
        if phase_id in seen_ids:
            # Skip duplicate headings — sometimes the architect repeats a phase heading
            # in a summary section.
            continue
        seen_ids.add(phase_id)
        title = (m.group("title") or "").strip() or phase_id
        phases.append(ParsedPhase(id=phase_id, title=title))

    # Sort by numeric phase id in case they appeared out of order
    phases.sort(key=lambda p: int(p.id[1:]))
    return phases
