"""Tool errors: the message is returned to the model as an error result (SPEC §5.4).

Because these messages are agent-visible, they must stay mechanism-only:
no budget, spend, horizon, or session-cap information (D12), and no
strategy hints (hard rule 2).
"""


class ToolError(Exception):
    """A tool call failed; str(exc) becomes the is_error tool result."""
