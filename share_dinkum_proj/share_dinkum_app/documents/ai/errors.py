class AIError(Exception):
    """The AI could not read a document: unreachable, refused, or replied with something unusable."""


class AIConfigError(AIError):
    """The DOCUMENT_AI_* settings do not name a usable provider."""
