"""Repetition breaker: silent session-ending rules evaluated after every tool call.

A sibling of ``session_tool_cap`` (D13 semantics): the agent is never
told the rules exist, tripping is silent (no warning, no final model
call), and the session ends exactly as ``tool_cap`` does. Three
mechanical rules, each with a manifest-pinned knob (SPEC §9 caps):

- **identical_call** (``repetition_identical_cap``, default 5): the same
  signature — tool name + normalized-args hash — executed that many
  times within a session, regardless of success/error.
- **window_diversity** (``repetition_window`` 30 /
  ``repetition_min_distinct`` 4): over the last ``window`` executed
  calls the number of distinct signatures is at or below the floor —
  the session is cycling a small read-set. Evaluated only once the
  window is full. Note: with the default knobs the identical-call rule
  dominates (any full 30-call window with <= 4 distinct signatures
  contains a signature repeated >= 8 >= 5 times, which tripped earlier);
  the rule is live mechanism for manifests that pin a higher
  identical cap.
- **same_tool_errors** (``repetition_same_tool_error_cap``, default 8):
  that many consecutive executed calls of the same tool (args may
  differ) all classified error-or-revert. On-chain reverts surface as
  success-shaped harness results (``isError`` false, ``status:
  "reverted"`` or an ``error`` field in the JSON content) and so never
  advance ``max_consecutive_errors`` — this rule is what catches
  parameter-sweep revert loops.

Rules are evaluated in the order above; the first to trip names the
session_end telemetry fields. Nothing here is agent-visible.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any

RULE_IDENTICAL = "identical_call"
RULE_WINDOW = "window_diversity"
RULE_SAME_TOOL_ERRORS = "same_tool_errors"

DEFAULT_IDENTICAL_CAP = 5
DEFAULT_WINDOW = 30
DEFAULT_MIN_DISTINCT = 4
DEFAULT_SAME_TOOL_ERROR_CAP = 8


def signature(name: str, args: dict[str, Any]) -> str:
    """``tool:hash12`` — the tool name plus a normalized-args digest.

    Normalization is canonical JSON (sorted keys, tight separators), so
    key order never distinguishes two otherwise-identical calls.
    """
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"{name}:{digest}"


def is_error_or_revert(ok: bool, content: str) -> bool:
    """Classify one executed call for the same_tool_errors rule.

    Loop-level failures (``ok`` false) always count. Success-shaped
    results count when their JSON content carries the harness's revert
    or error markers — top-level (or one ``result`` level down, the same
    nesting the tx_hash extractor tolerates) ``status: "reverted"`` or a
    non-empty ``error`` field. Non-JSON success content never counts.
    """
    if not ok:
        return True
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return False
    candidates = [parsed]
    if isinstance(parsed, dict) and isinstance(parsed.get("result"), dict):
        candidates.append(parsed["result"])
    for candidate in candidates:
        if isinstance(candidate, dict):
            if candidate.get("status") == "reverted":
                return True
            if candidate.get("error"):
                return True
    return False


@dataclass(frozen=True, slots=True)
class RepetitionTrip:
    """One tripped rule: the rule name plus its session_end telemetry fields."""

    rule: str
    fields: dict[str, Any]


@dataclass
class RepetitionTracker:
    """Per-session state for the three rules; ``record`` after every executed call."""

    identical_cap: int = DEFAULT_IDENTICAL_CAP
    window: int = DEFAULT_WINDOW
    min_distinct: int = DEFAULT_MIN_DISTINCT
    same_tool_error_cap: int = DEFAULT_SAME_TOOL_ERROR_CAP

    _counts: Counter[str] = field(default_factory=Counter)
    _recent: deque[str] = field(default_factory=deque)
    _streak_tool: str | None = None
    _streak: int = 0

    def record(
        self, name: str, args: dict[str, Any], *, error_or_revert: bool
    ) -> RepetitionTrip | None:
        """Fold one executed call in; return the trip if any rule fires."""
        sig = signature(name, args)
        self._counts[sig] += 1
        self._recent.append(sig)
        if len(self._recent) > self.window:
            self._recent.popleft()
        if error_or_revert:
            if name == self._streak_tool:
                self._streak += 1
            else:
                self._streak_tool = name
                self._streak = 1
        else:
            self._streak_tool = None
            self._streak = 0

        if self._counts[sig] >= self.identical_cap:
            return RepetitionTrip(
                RULE_IDENTICAL,
                {"repetition_signature": sig, "repetition_count": self._counts[sig]},
            )
        if len(self._recent) >= self.window:
            distinct = sorted(set(self._recent))
            if len(distinct) <= self.min_distinct:
                return RepetitionTrip(
                    RULE_WINDOW,
                    {
                        "repetition_window": self.window,
                        "repetition_distinct": len(distinct),
                        "repetition_signatures": distinct,
                    },
                )
        if self._streak >= self.same_tool_error_cap:
            return RepetitionTrip(
                RULE_SAME_TOOL_ERRORS,
                {"repetition_tool": name, "repetition_count": self._streak},
            )
        return None
