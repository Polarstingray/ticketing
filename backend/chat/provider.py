"""One OpenAI-compatible chat completion.

The non-streaming ancestor of this code is the resolver's ``_chat_completion``
(``resolver/resolve_tickets.py``), and the response parsing and quota handling
carry over from it directly. Two things differ, both because this one serves a
browser request rather than a batch sweep:

* Failures raise :class:`ProviderError` with a *user-facing* message and an HTTP
  status for the router, instead of returning an ``(ok, text)`` tuple for a log.
* Nothing is teed to disk. The resolver logs transcripts for later forensics; a
  chat turn belongs to a user and is persisted (from the next phase) as their
  conversation, not as a file on the server.

The call is synchronous on purpose. Every route in this app is a plain ``def``,
which FastAPI runs in a threadpool — so a blocking HTTP call here occupies a
worker thread and never the event loop, and the module stays consistent with the
synchronous SQLAlchemy session it sits beside.
"""
from dataclasses import dataclass

import httpx

from .config import ChatConfig

# Providers vary in what they call these; normalize to the OpenAI names we read.
_USAGE_INPUT_KEYS = ("prompt_tokens", "input_tokens")
_USAGE_OUTPUT_KEYS = ("completion_tokens", "output_tokens")


class ProviderError(Exception):
    """A chat completion that could not be produced.

    ``status`` is the HTTP status the router should return. Provider faults map
    to 502 (the app is fine; its upstream is not) and quota exhaustion to 429, so
    a client can distinguish "try again later" from "this is broken". Rejected
    credentials are the exception: they map to 500, because a key the provider
    will not accept is this deployment's misconfiguration, not an upstream fault.
    """

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    input_tokens: int
    output_tokens: int


def _usage_value(usage: dict, keys: tuple[str, ...]) -> int:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return 0


def complete(cfg: ChatConfig, system: str, user_message: str) -> Completion:
    """Send one completion and return the assistant's reply.

    Raises :class:`ProviderError` for every failure mode — transport, non-200,
    unparseable body, empty completion — so the caller has exactly one thing to
    catch and no ``ok`` flag to forget to check.
    """
    body = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_message},
        ],
    }
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }
    try:
        resp = httpx.post(cfg.api_url, json=body, headers=headers, timeout=cfg.timeout)
    except httpx.TimeoutException:
        raise ProviderError(
            f"The model did not respond within {cfg.timeout}s.", status=504
        ) from None
    except httpx.HTTPError as exc:
        # The exception text can carry the URL, which `public()` deliberately
        # withholds from clients — so report the class of failure, not the detail.
        raise ProviderError(f"Could not reach the model provider: {type(exc).__name__}.") from None

    if resp.status_code == 429:
        # Distinct from the app's own per-IP limit: this is the *provider's*
        # quota, and waiting is the only remedy the user has.
        raise ProviderError(
            "The model provider is rate-limiting requests. Try again shortly.",
            status=429,
        )
    if resp.status_code in (401, 403):
        # 500, not 502: the upstream answered fine, it just refused *our* key. The
        # fault is this deployment's configuration, and 502 would send an operator
        # hunting through the provider's logs for an outage that isn't there.
        #
        # Never echo the provider's body here — misconfigured gateways have been
        # known to reflect the submitted credential back in the error.
        raise ProviderError(
            "The model provider rejected this deployment's credentials.", status=500
        )
    if resp.status_code != 200:
        raise ProviderError(
            f"The model provider returned HTTP {resp.status_code}.", status=502
        )

    try:
        data = resp.json()
        text = (data["choices"][0]["message"]["content"] or "").strip()
    except (ValueError, KeyError, IndexError, TypeError):
        raise ProviderError("Could not parse the model provider's response.") from None

    if not text:
        raise ProviderError("The model returned an empty response.")

    usage = data.get("usage") if isinstance(data, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    return Completion(
        text=text,
        # Prefer the model the provider says it served: a gateway may resolve an
        # alias ("llama-3.1-70b") to a specific build, and the served id is the
        # one worth recording against the cost.
        model=str(data.get("model") or cfg.model),
        input_tokens=_usage_value(usage, _USAGE_INPUT_KEYS),
        output_tokens=_usage_value(usage, _USAGE_OUTPUT_KEYS),
    )
