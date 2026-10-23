"""The AI fallback: reads a document no parser recognises, with whichever vendor the settings name.

Only the transport is the vendor's (`adapters`). The prompt, the output schema, reading the reply and
checking it are the app's, and the events it gives are validated like a parser's. It is off unless
DOCUMENT_AI_PROVIDER names a provider and the command is run with `--ai`.

Every document is redacted before it is sent, local model or not: the terms in `redact_terms.txt`
and the identifier masks, as `redact_documents` does. The figures and references it needs are kept.
"""

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings

from share_dinkum_app.documents import columns, readers, redaction
from share_dinkum_app.documents.ai import adapters, cache as cache_module, schema
from share_dinkum_app.documents.ai.errors import AIConfigError, AIError
from share_dinkum_app.documents.extracted import Document, Extracted

__all__ = ['AIConfigError', 'AIError', 'Reader', 'from_settings']

INSTRUCTIONS = """\
You read Australian share documents and return the events in them as JSON, exactly in the schema given.
A document is a payment advice (a dividend or distribution), a broker's contract note (a buy or sell),
an annual tax statement from a trust or ETF (an AMMA statement), or a broker's export of several of these.

Rules:
- One event per payment, trade or statement. A company dividend is a Dividend; a trust or ETF
  distribution is a Distribution. An annual statement is one AttributionStatement, followed by one
  AttributionComponent per line it shows, and a CostBaseAdjustment if it states an AMIT cost base net
  amount (positive for an increase, negative for a decrease).
- Write every number as text, exactly as printed, without $, commas or spaces, and keep every decimal
  place. Never round, and never work a value out from other values.
- A rate printed in cents per unit is given in dollars: 143.6788 cents is "1.436788".
- Dates are YYYY-MM-DD. A payment's date is the payment date; a trade's is the trade date, not the
  settlement date.
- total_brokerage is brokerage, GST and every other fee on the trade added together.
- instrument__name is the exchange code as printed, in upper case, such as VAS or BHP.
- Each money column's _currency column holds a currency code such as AUD or USD.
- stated_total is the total the document prints for the event before any tax or fee is taken off (the
  gross payment, or units times price for a trade), or null. reference is the document's own payment or
  contract note number, or null.
- Use null for anything the document does not state. If the document is none of these, return
  {"events": []}.

The columns of each model:

"""


def system_prompt() -> str:
    return INSTRUCTIONS + columns.markdown(columns.MODELS)


@dataclass
class Reader:
    provider: str
    model: str
    server: str
    adapter: adapters.Adapter
    cache: cache_module.Cache
    # The user's own terms to take out, from redact_terms.txt. The identifier masks apply regardless.
    terms: list[redaction.Term] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f'ai:{self.provider}/{self.model}'

    @property
    def is_local(self) -> bool:
        """Whether the documents' text stays on this machine."""
        return self.provider == 'openai_compatible' and urlparse(self.server).hostname in ('localhost', '127.0.0.1',
                                                                                           '::1')

    def extract(self, doc: Document) -> list[Extracted]:
        """The events the AI reads in `doc`. Raises AIError if it cannot."""
        system = system_prompt()
        text = redaction.redact(readers.as_text(doc), self.terms).text
        cache_key = cache_module.key(readers.sha256(doc.path), self.provider, self.model, self.server, system)
        reply = self.cache.get(cache_key)
        if reply is None:
            reply = self._ask(system, text)
            self.cache.put(cache_key, reply, str(redaction.redact_path(Path(doc.path.name), self.terms)), self.label)
        try:
            return schema.parse_reply(reply)
        except schema.ReplyError as error:
            raise AIError(f'The kept reply could not be used: {error}') from None

    def _ask(self, system: str, text: str) -> str:
        """A reply that reads as events, asking once more with what was wrong if the first does not."""
        output_schema = schema.output_schema()
        user = f'The document:\n\n{text}'
        reply = self.adapter.complete(system, user, output_schema)
        try:
            schema.parse_reply(reply)
            return reply
        except schema.ReplyError as error:
            retry = (f'{user}\n\nYour previous reply was:\n{reply}\n\nIt could not be used: {error}\n'
                     f'Reply again with corrected JSON only.')
        reply = self.adapter.complete(system, retry, output_schema)
        try:
            schema.parse_reply(reply)
        except schema.ReplyError as error:
            raise AIError(f'The reply could not be used, twice: {error}') from None
        return reply


def from_settings() -> Reader | None:
    """The reader the DOCUMENT_AI_* settings name, or None when DOCUMENT_AI_PROVIDER is 'none'.

    It redacts with `redact_terms.txt` when there is one. Raises AIConfigError for a setting that
    cannot work.
    """
    provider = (settings.DOCUMENT_AI_PROVIDER or 'none').strip()
    if provider == 'none':
        return None
    adapter = adapters.get(provider, settings.DOCUMENT_AI_MODEL, settings.DOCUMENT_AI_BASE_URL,
                           settings.DOCUMENT_AI_API_KEY)
    model = getattr(adapter, 'model', settings.DOCUMENT_AI_MODEL)
    terms_path = redaction.default_terms_path()
    terms = redaction.load_terms(terms_path) if terms_path.is_file() else []
    return Reader(provider=provider, model=model, server=settings.DOCUMENT_AI_BASE_URL, adapter=adapter,
                  cache=cache_module.Cache(settings.DOCUMENT_AI_CACHE), terms=terms)
