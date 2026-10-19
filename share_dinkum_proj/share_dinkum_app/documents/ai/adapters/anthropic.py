"""Claude, through Anthropic's own SDK (`uv sync --extra anthropic`)."""

from typing import Any

from share_dinkum_app.documents.ai.errors import AIConfigError, AIError

DEFAULT_MODEL = 'claude-opus-5-5'
MAX_TOKENS = 16000
# Models that take `fallbacks`: a request one declines is answered by another model instead.
FALLBACK_MODELS = {'claude-fable-5-1', 'claude-opus-5-5', 'claude-opus-5', 'claude-sonnet-5-5'}
FALLBACK_BETA = 'server-side-fallback-2026-07-01'


class Anthropic:
    def __init__(self, model: str, api_key: str, base_url: str) -> None:
        try:
            import anthropic
        except ImportError:
            raise AIConfigError('The anthropic package is not installed. Run: uv sync --extra anthropic') from None
        self.sdk = anthropic
        # With no key set here, the SDK finds one itself (ANTHROPIC_API_KEY, or an `ant auth login` profile).
        self.client = anthropic.Anthropic(api_key=api_key or None, base_url=base_url or None)
        self.model = model or DEFAULT_MODEL

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> str:
        options: dict[str, Any] = {}
        if self.model in FALLBACK_MODELS:
            options = {'betas': [FALLBACK_BETA], 'fallbacks': 'default'}
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=[{'role': 'user', 'content': user}],
                output_config={'format': {'type': 'json_schema', 'schema': schema}},
                **options,
            )
        except self.sdk.APIConnectionError as error:
            raise AIError(f'Could not reach the Anthropic API: {error}') from None
        except self.sdk.APIStatusError as error:
            raise AIError(f'The Anthropic API answered {error.status_code}: {error.message}') from None

        if response.stop_reason == 'refusal':
            raise AIError('Claude declined to read this document.')
        if response.stop_reason == 'max_tokens':
            raise AIError('The reply was cut off before it finished.')
        text = next((block.text for block in response.content if block.type == 'text'), None)
        if text is None:
            raise AIError('The reply had no text in it.')
        return text
