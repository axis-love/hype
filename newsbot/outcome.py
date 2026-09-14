"""Job outcome enum — replaces 0/1/2/3/4 result codes.

Mapping: 0→OK, 1→FAILED, 2→BUSY, 3→NOTHING_TO_DO, 4→BELOW_THRESHOLD.

3 used to mean both "no progress" (generation) and "store empty" (posting);
both are NOTHING_TO_DO. Slot consumption is explicit via consumes_slot.
exit_code is OK→0 else 1, used only by main() for --once.
"""
from __future__ import annotations

import enum


class Outcome(enum.Enum):
    OK = "ok"
    FAILED = "failed"
    BUSY = "busy"
    NOTHING_TO_DO = "nothing_to_do"
    BELOW_THRESHOLD = "below_threshold"

    @property
    def consumes_slot(self) -> bool:
        """True for outcomes that consume a posting (or summary) slot."""
        return self in (Outcome.OK, Outcome.NOTHING_TO_DO, Outcome.BELOW_THRESHOLD)

    @property
    def exit_code(self) -> int:
        """Process exit code: 0 on OK, 1 otherwise. Used only by main() --once."""
        return 0 if self is Outcome.OK else 1
