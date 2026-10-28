from dataclasses import dataclass


@dataclass(frozen=True)
class ToolContent:
    """Explicit multimodal output; ordinary Python lists remain JSON data."""
    content: list[dict]
