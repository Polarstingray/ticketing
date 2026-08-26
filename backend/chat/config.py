"""Provider settings for the chat assistant, read from the environment.

Mirrors the resolver's ``REVIEW_API_*`` trio (see ``resolver/config.py``): an
OpenAI-compatible ``/chat/completions`` endpoint, a bearer key and a model id.
That shape works against Anthropic, Groq, Mistral, OpenRouter, a local llama.cpp
server — anything speaking the common dialect — so the app is not tied to one
vendor.

**Secrets live here and only here.** Unlike the resolver's non-secret tunables
(``ResolverSettings``), none of this is stored in the database or exposed by the
API; ``ChatConfig.public()`` is the redacted view the UI is allowed to see.

The feature is off unless URL, key and model are *all* set — the same
all-or-nothing check the resolver uses for its single-shot review path. With any
of them missing the router reports itself disabled and refuses to answer, so a
stock deployment carries no trace of the feature.
"""
import logging
import os
from dataclasses import dataclass
from functools import lru_cache

logger = logging.getLogger(__name__)

# Read once per process (``lru_cache`` below). Tests that need a different
# configuration call ``load.cache_clear()`` after patching the environment.


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _warn_unparseable(name: str, raw: str, default) -> None:
    """Say so when a tunable was ignored — the value is a number, never a secret.

    Only the numeric tunables are logged this way. ``CHAT_API_KEY`` and
    ``CHAT_API_URL`` never pass through here.
    """
    logger.warning(
        "%s=%r is not a number; falling back to %r", name, raw, default
    )


def _float_env(name: str, default: float) -> float:
    """A float from the environment, falling back on anything unparseable.

    Pricing and limits are operator-typed; a typo should degrade the cost column
    to zero, not stop the app from booting — but it warns, so the zero is not
    mistaken for a deliberate setting.
    """
    raw = _env(name)
    try:
        return float(raw or default)
    except ValueError:
        _warn_unparseable(name, raw, default)
        return default


def _int_env(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw or default)
    except ValueError:
        _warn_unparseable(name, raw, default)
        return default


def _bool_env(name: str, default: bool) -> bool:
    """A boolean from the environment. Unset ⇒ ``default``; anything else is
    read leniently, since operators write ``1``, ``yes`` and ``on`` too."""
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class ChatConfig:
    api_url: str
    api_key: str
    model: str
    timeout: int
    # Characters (not tokens) of ticket context to include. A character budget is
    # deliberate: it needs no tokenizer, is provider-independent, and errs small.
    context_budget: int
    # USD per 1M tokens, used only to price the answer for display. Left at 0 the
    # cost column reads $0.00 rather than lying with a wrong number.
    price_in_per_mtok: float
    price_out_per_mtok: float
    rate_limit: str
    # Per-user USD ceiling per UTC day, summed over stored turns. 0 disables it.
    # A per-IP rate limit bounds *bursts*; this bounds the bill.
    daily_usd_limit: float
    # How many prior turns of a thread to replay to the model. Threads are
    # unbounded, provider context windows are not, and every replayed turn is
    # re-billed on each new question.
    history_turns: int
    # Ask the provider to report token usage on the final streaming chunk
    # (OpenAI's `stream_options`). Most compatible gateways accept or ignore it;
    # a strict one rejects the unknown field outright, hence the escape hatch.
    stream_usage: bool

    @property
    def enabled(self) -> bool:
        """All three of URL/key/model, or the feature is off.

        Partial configuration is treated as *off* rather than as an error: a
        half-set environment is far more likely to be an in-progress rollout than
        an intent to serve broken requests.
        """
        return bool(self.api_url and self.api_key and self.model)

    def public(self) -> dict:
        """The redacted view safe to hand a browser — never the key or the URL.

        The endpoint URL is withheld along with the key: it is not a credential,
        but on a self-hosted provider it is internal topology, and the client has
        no use for it.
        """
        return {
            "enabled": self.enabled,
            "model": self.model if self.enabled else "",
            "daily_usd_limit": self.daily_usd_limit,
        }


@lru_cache(maxsize=1)
def load() -> ChatConfig:
    return ChatConfig(
        api_url=_env("CHAT_API_URL"),
        api_key=_env("CHAT_API_KEY"),
        model=_env("CHAT_API_MODEL"),
        timeout=_int_env("CHAT_TIMEOUT", 120),
        context_budget=_int_env("CHAT_CONTEXT_BUDGET", 60_000),
        price_in_per_mtok=_float_env("CHAT_PRICE_IN", 0.0),
        price_out_per_mtok=_float_env("CHAT_PRICE_OUT", 0.0),
        rate_limit=_env("CHAT_RATE_LIMIT") or "20/minute",
        daily_usd_limit=_float_env("CHAT_DAILY_USD_LIMIT", 0.0),
        history_turns=_int_env("CHAT_HISTORY_TURNS", 10),
        stream_usage=_bool_env("CHAT_STREAM_USAGE", True),
    )
