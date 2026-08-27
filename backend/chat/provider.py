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
import json
from collections.abc import Iterator
from dataclasses import dataclass, field

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

    if resp.status_code != 200:
        # Never echo the provider's body — misconfigured gateways have been known
        # to reflect the submitted credential back in an auth error. `429` stays
        # `429`: that is the *provider's* quota, distinct from our per-IP limit,
        # and waiting is the only remedy the user has.
        raise _status_error(resp.status_code)

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


# --- Streaming ---------------------------------------------------------------
# The same endpoint with ``stream: true``, which answers in Server-Sent Events:
# a sequence of ``data: {json}`` lines carrying incremental deltas, terminated by
# the literal ``data: [DONE]``. We re-emit the text deltas to our own client and
# keep the usage block, when the provider sends one, for the cost record.

_SSE_DATA_PREFIX = "data:"
_SSE_DONE = "[DONE]"


@dataclass(frozen=True)
class ToolCall:
    """One complete tool call the model asked for.

    ``arguments`` is the raw JSON *text* exactly as the model emitted it, not a
    parsed dict. It is kept verbatim because it has to be echoed back in the
    assistant message on the next hop, and some providers validate that
    round-trip — re-serializing a parsed dict changes key order and whitespace.
    Parsing happens once, at dispatch, where a failure is answerable.
    """
    id: str
    name: str
    arguments: str


@dataclass
class StreamResult:
    """What a finished stream knew by the end of it.

    Populated as the stream is consumed and read by the caller afterwards, so the
    generator can stay a plain text iterator while still reporting usage.
    ``model`` and the token counts are zero/empty when the provider declined to
    report them — which is why cost can legitimately be 0.0 on a real answer.

    ``tool_calls`` belongs here rather than in the yielded stream for the same
    reason usage does: a tool call arrives in fragments (id, name and arguments
    each split across chunks) and a *partial* one cannot be dispatched, displayed
    or even parsed. The only meaningful moment for it is the end, which is
    precisely what this dataclass means.
    """
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    # Recorded for diagnostics, deliberately never branched on: several
    # OpenAI-compatible gateways report "stop" on a chunk that carries tool calls.
    # The presence of accumulated calls is the ground truth, not this.
    finish_reason: str = ""


def _delta_text(chunk: dict) -> str:
    """The incremental content in one streaming chunk, or "" if it carries none.

    A chunk may legitimately have no choices at all — the usage-only final chunk
    is exactly that shape — so every level is probed defensively rather than
    indexed.
    """
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    delta = first.get("delta")
    if not isinstance(delta, dict):
        return ""
    content = delta.get("content")
    return content if isinstance(content, str) else ""


def _accumulate_tool_calls(chunk: dict, buffers: dict) -> None:
    """Fold one chunk's ``delta.tool_calls`` fragments into ``buffers``.

    Every level is probed defensively, exactly like :func:`_delta_text` — the
    usage-only final chunk carries no choices at all.

    Keying is the subtle part. OpenAI keys parallel calls by ``index``, but not
    every compatible gateway sends one; falling back to ``id`` matters because
    defaulting a missing index to 0 would silently *merge two parallel calls into
    one corrupt blob*, which fails as a confusing JSON parse error much later.
    """
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return
    first = choices[0]
    if not isinstance(first, dict):
        return
    delta = first.get("delta")
    if not isinstance(delta, dict):
        return
    entries = delta.get("tool_calls")
    if not isinstance(entries, list):
        return

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        index = entry.get("index")
        call_id = entry.get("id")
        if isinstance(index, int):
            key = ("i", index)
        elif isinstance(call_id, str) and call_id:
            key = ("id", call_id)
        else:
            key = ("i", 0)
        if key not in buffers:
            # `seq` preserves the order calls were first seen, which is what
            # orders id-keyed calls (they have no index to sort by).
            buffers[key] = {"id": "", "name": "", "arguments": "", "seq": len(buffers)}
        buf = buffers[key]

        # The id repeats as "" on continuation fragments; first non-empty wins.
        if isinstance(call_id, str) and call_id and not buf["id"]:
            buf["id"] = call_id
        function = entry.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if isinstance(name, str):
            # Appended, not assigned: the wire format permits a split name, and
            # assigning would keep only its last fragment. No gateway observed
            # repeats a whole name across fragments, which is what would make
            # appending wrong.
            buf["name"] += name
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            buf["arguments"] += arguments


def _finish_tool_calls(buffers: dict) -> list[ToolCall]:
    """The accumulated buffers as complete calls, in the order they were opened.

    A buffer with no name is dropped: a stream cut off mid-call leaves one, and a
    nameless call cannot be dispatched — passing it on would only turn a truncated
    answer into an "unknown tool" error attributed to the model.
    """
    def order(item):
        (kind, value), buf = item
        # Numeric on the index when there is one — string-sorting it would put
        # call 10 before call 2. Otherwise first-seen order.
        return (0, value) if kind == "i" else (1, buf["seq"])

    return [
        ToolCall(id=buf["id"], name=buf["name"], arguments=buf["arguments"])
        for _, buf in sorted(buffers.items(), key=order)
        if buf["name"]
    ]


def stream(cfg: ChatConfig, system: str, messages: list[dict],
           result: StreamResult, *, tools: list[dict] | None = None) -> Iterator[str]:
    """Yield the assistant's reply in fragments as the provider produces it.

    ``result`` is filled in as the stream is consumed — the caller reads it once
    the generator is exhausted to record the model and token usage. It is passed
    in rather than returned because a generator's return value is awkward to
    reach, and the caller needs the numbers even when the stream ends early.

    Raises :class:`ProviderError` for pre-stream failures (transport, non-200) —
    the same contract as :func:`complete`. A failure *mid-stream* cannot be
    signalled that way without discarding the text already yielded, so it ends
    the iteration instead and leaves ``result.text`` holding the partial answer;
    the router reports that as a truncated turn rather than a lost one.

    ``tools`` is keyword-only and defaults to ``None``, and when it is falsy the
    request body is byte-identical to the pre-tools one — so the text-only path
    (and every caller that predates tools) is unaffected. When tools *are*
    declared the model may answer with tool calls instead of text; those land on
    ``result.tool_calls`` rather than being yielded, since a partial call is not
    a renderable thing. See :func:`_accumulate_tool_calls`.
    """
    body = {
        "model": cfg.model,
        "messages": [{"role": "system", "content": system}] + messages,
        "stream": True,
    }
    if tools:
        # Same discipline as `stream_options` below: added only when it applies,
        # so a strict gateway on the text-only path never sees an unknown field.
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if cfg.stream_usage:
        # OpenAI's opt-in for a final usage chunk. Compatible gateways accept or
        # ignore it; CHAT_STREAM_USAGE=false is the escape hatch for a strict one
        # that rejects the unknown field.
        body["stream_options"] = {"include_usage": True}
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }

    tool_buffers: dict = {}
    try:
        with httpx.stream("POST", cfg.api_url, json=body, headers=headers,
                          timeout=cfg.timeout) as resp:
            if resp.status_code != 200:
                # The body must be read explicitly on a streaming response before
                # it can be inspected — but we only ever use its status, never
                # its text (see complete()).
                resp.read()
                raise _status_error(resp.status_code)
            for line in resp.iter_lines():
                if not line or not line.startswith(_SSE_DATA_PREFIX):
                    continue  # comments, keep-alives and blank separators
                payload = line[len(_SSE_DATA_PREFIX):].strip()
                if payload == _SSE_DONE:
                    break
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue  # a malformed frame loses one delta, not the answer
                if not isinstance(chunk, dict):
                    continue
                if chunk.get("model"):
                    result.model = str(chunk["model"])
                usage = chunk.get("usage")
                if isinstance(usage, dict):
                    result.input_tokens = _usage_value(usage, _USAGE_INPUT_KEYS)
                    result.output_tokens = _usage_value(usage, _USAGE_OUTPUT_KEYS)
                choices = chunk.get("choices")
                if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                    reason = choices[0].get("finish_reason")
                    if isinstance(reason, str) and reason:
                        result.finish_reason = reason
                if tools:
                    _accumulate_tool_calls(chunk, tool_buffers)
                text = _delta_text(chunk)
                if text:
                    result.text += text
                    yield text
    except httpx.TimeoutException:
        raise ProviderError(
            f"The model did not respond within {cfg.timeout}s.", status=504
        ) from None
    except httpx.HTTPError as exc:
        raise ProviderError(
            f"Could not reach the model provider: {type(exc).__name__}."
        ) from None

    result.tool_calls = _finish_tool_calls(tool_buffers)
    if not result.model:
        result.model = cfg.model


def _status_error(status_code: int) -> ProviderError:
    """Map an upstream status to a ProviderError. Shared by both call paths so
    the streaming and non-streaming routes can't drift on what a given upstream
    status means — a rejected key is a 500 on both, a quota a 429 on both."""
    if status_code == 429:
        return ProviderError(
            "The model provider is rate-limiting requests. Try again shortly.",
            status=429,
        )
    if status_code in (401, 403):
        # 500, not 502: the upstream answered fine, it just refused *our* key. The
        # fault is this deployment's configuration, and 502 would send an operator
        # hunting through the provider's logs for an outage that isn't there.
        return ProviderError(
            "The model provider rejected this deployment's credentials.", status=500
        )
    return ProviderError(
        f"The model provider returned HTTP {status_code}.", status=502
    )
