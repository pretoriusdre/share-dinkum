"""One module per vendor. Each turns a prompt and an output schema into the reply's text.

Everything else (the prompt, the schema, reading the reply, checking it) is the app's own, so adding
a vendor means adding a module here with one class and naming it in `get`.
"""

from typing import Any, Protocol

from share_dinkum_app.documents.ai.errors import AIConfigError

PROVIDERS = ('openai_compatible', 'anthropic')


class Adapter(Protocol):
    def complete(self, system: str, user: str, schema: dict[str, Any]) -> str:
        """The reply's text: JSON matching `schema`, or as near as the vendor manages."""
        ...


def get(provider: str, model: str, base_url: str, api_key: str) -> Adapter:
    """The adapter for `provider`. Raises AIConfigError for an unknown one or a missing setting."""
    if provider == 'openai_compatible':
        from share_dinkum_app.documents.ai.adapters.openai_compatible import OpenAICompatible

        return OpenAICompatible(base_url=base_url, model=model, api_key=api_key)
    if provider == 'anthropic':
        from share_dinkum_app.documents.ai.adapters.anthropic import Anthropic

        return Anthropic(model=model, api_key=api_key, base_url=base_url)
    raise AIConfigError(f'DOCUMENT_AI_PROVIDER is {provider!r}. It must be none, or one of: {", ".join(PROVIDERS)}.')
