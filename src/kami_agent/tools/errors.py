"""Tool errors: the message is returned to the model as an error result (SPEC P2).

Because these messages are agent-visible, they must stay mechanism-only:
no budget, spend, horizon, or session-cap information (I1), and no
strategy hints (I3).
"""


class ToolError(Exception):
    """A tool call failed; str(exc) becomes the is_error tool result."""
