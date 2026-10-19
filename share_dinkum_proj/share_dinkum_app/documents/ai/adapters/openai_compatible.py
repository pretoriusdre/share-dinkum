"""Any server with the `/v1/chat/completions` shape: OpenAI, most hosted vendors, and local servers
such as Ollama (`http://localhost:11434/v1`), LM Studio, llama.cpp and vLLM.
"""

from typing import Any

import requests

from share_dinkum_app.documents.ai.errors import AIConfigError, AIError

TIMEOUT_SECONDS = 600


class OpenAICompatible:
    def __init__(self, base_url: str, model: str, api_key: str) -> None:
        if not base_url:
            raise AIConfigError('Set DOCUMENT_AI_BASE_URL, such as http://localhost:11434/v1 for Ollama.')
        if not model:
            raise AIConfigError('Set DOCUMENT_AI_MODEL to the model to use, such as qwen3:30b.')
        self.url = f'{base_url.rstrip("/")}/chat/completions'
        self.model = model
        self.api_key = api_key

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> str:
        body: dict[str, Any] = {
            'model': self.model,
            'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
            'temperature': 0,
            'response_format': {'type': 'json_schema', 'json_schema': {'name': 'events', 'schema': schema}},
        }
        response = self._post(body)
        if response.status_code == 400:
            # A server that cannot hold a reply to a schema is asked for plain JSON; the reply is
            # checked against the schema either way.
            response = self._post({**body, 'response_format': {'type': 'json_object'}})
        if not response.ok:
            raise AIError(f'{self.url} answered {response.status_code}: {response.text[:300]}')
        try:
            choice = response.json()['choices'][0]
            content = choice['message']['content']
        except (ValueError, KeyError, IndexError, TypeError):
            raise AIError(f'{self.url} gave a reply in an unexpected shape: {response.text[:300]}') from None
        if choice.get('finish_reason') == 'length':
            raise AIError('The reply was cut off before it finished. A local server may need a larger context '
                          'window (Ollama: OLLAMA_CONTEXT_LENGTH).')
        return str(content or '')

    def _post(self, body: dict[str, Any]) -> requests.Response:
        headers = {'Authorization': f'Bearer {self.api_key}'} if self.api_key else {}
        try:
            return requests.post(self.url, json=body, headers=headers, timeout=TIMEOUT_SECONDS)
        except requests.RequestException as error:
            raise AIError(f'Could not reach {self.url}: {error}. Is the server running?') from None
