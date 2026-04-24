"""System prompts for each agent role. Single source of truth — docs/PROMPTS.md mirrors these."""

from .library import (
    REFLECTIVE_PRACTICE_BLOCK,
    architect_prompt,
    coder_prompt,
    dispatcher_prompt,
    reviewer_prompt,
)

__all__ = [
    "REFLECTIVE_PRACTICE_BLOCK",
    "architect_prompt",
    "coder_prompt",
    "dispatcher_prompt",
    "reviewer_prompt",
]
