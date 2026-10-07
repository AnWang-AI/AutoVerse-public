"""Canonical summary template for meta-compaction (docs/architecture/runtime-flow.md).

Kept in its own module so the CompactionConfig default can stay empty and fall
back to this at runtime (avoids import-order coupling). The section titles are
the full §4.5a set the summariser is asked to fill in.
"""

from __future__ import annotations

SUMMARY_TEMPLATE = """\
You are compacting a long conversation into a compact, faithful summary so the
assistant can continue working with full context. Preserve concrete facts
verbatim (file paths, identifiers, numbers, commands, decisions). Do not invent
information. Produce the summary under the following sections:

## ORIGINAL GOAL
The user's overarching objective, copied as faithfully as possible.

## NARRATIVE
A prose account of what has happened so far, in order.

## KEY FACTS
Specific values that must survive: file paths, identifiers, config values,
error messages, command outputs.

## DECISIONS
Choices made and their rationale.

## OPEN THREADS
Work that is in progress or pending, and the immediate next step.

## CHAPTERS
One line per logical phase of the work.
"""


__all__ = ["SUMMARY_TEMPLATE"]
