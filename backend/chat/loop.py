"""The multi-hop turn: stream, dispatch tools, stream again, answer.

One question can now cost several provider calls. The model answers with tool
calls instead of text, we run them against the *caller's* permissions, append the
results, and ask again — until it answers in prose or runs out of hops.

Three boundaries are deliberate here:

* **This module knows nothing about SSE.** It yields ``(event_name, data)``
  pairs, which is exactly the argument pair ``routers.chat._sse`` takes. That
  keeps the framing in the router and lets the loop be tested without HTTP.
* **It knows nothing about identity.** ``dispatch`` arrives already bound to a
  ``(db, user)`` by the router; the loop only ever passes it a name and the
  model's own arguments. See ``chat/tools.py``.
* **What is streamed is what is stored.** ``state.text`` accumulates every
  fragment from every hop, including the preamble an intermediate hop emits
  before its tool calls ("Let me check that ticket."). The browser has already
  appended those, so dropping them would make a reloaded thread differ from what
  the user watched.
"""
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from . import provider
from . import tools as tools_module
from .budget import Budget, estimate_cost
from .config import ChatConfig

logger = logging.getLogger(__name__)

# Appended only if the model somehow still asks for tools on the final,
# tools-free call. Yielded as a token as well as stored, so live matches stored.
CAP_NOTE = "\n\n_(I stopped looking things up for this question and answered from what I had.)_"

BUDGET_EXHAUSTED = "Error: the context budget for this question is used up."


@dataclass
class TurnState:
    """Everything one turn accumulated, across however many hops it took.

    Passed in and mutated rather than returned, the same convention
    ``provider.StreamResult`` established — a generator's return value is
    awkward to reach, and the caller needs these numbers even when the stream
    ends early.
    """
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    text: str = ""
    # Both destined for ChatMessage.meta, which models.py already documents as
    # the migration-free home for exactly this.
    tool_calls: list[dict] = field(default_factory=list)
    proposed_actions: list[dict] = field(default_factory=list)
    hops: int = 0
    capped: bool = False

    def absorb(self, result: provider.StreamResult) -> None:
        """Fold one hop's usage in. Summed, because the bill is per call."""
        self.input_tokens += result.input_tokens
        self.output_tokens += result.output_tokens
        if result.model:
            self.model = result.model


def _summarize(name: str, content: str) -> str:
    """A one-line description of a tool result, for the UI disclosure.

    Never the result body. The frame feeds a collapsed "what I looked at" list;
    re-transmitting ticket content there would duplicate what the answer already
    covers and put untrusted text on a second surface for no gain.
    """
    if content.startswith("Error:"):
        return content.split("\n", 1)[0][:120]
    chars = len(content)
    if name == "search_tickets":
        rows = max(0, content.count("\n") - 1)  # minus the header separator
        return f"{rows} ticket{'' if rows == 1 else 's'}"
    if name == "propose_action":
        return "proposed an action"
    return f"{chars / 1000:.1f}k chars" if chars >= 1000 else f"{chars} chars"


def _assistant_turn(result: provider.StreamResult) -> dict:
    """The assistant message to replay on the next hop.

    ``arguments`` is echoed **verbatim** rather than re-serialized from the
    parsed dict: some providers validate that the round-trip is byte-identical,
    and re-serializing changes key order and whitespace.
    """
    return {
        "role": "assistant",
        "content": result.text or None,
        "tool_calls": [
            {"id": c.id, "type": "function",
             "function": {"name": c.name, "arguments": c.arguments}}
            for c in result.tool_calls
        ],
    }


def run(cfg: ChatConfig, system: str, messages: list[dict], state: TurnState, *,
        dispatch: Callable[[str, dict], str],
        tools: list[dict],
        budget: Budget,
        spent_before: float = 0.0) -> Iterator[tuple[str, dict]]:
    """Run one turn to completion, yielding ``(event, data)`` as it goes.

    Events are ``token``, ``tool_call`` and ``tool_result``; the router frames
    them and adds its own ``done``.

    ``budget`` is shared across *all* hops. Without it six ``get_ticket`` calls
    on six large tickets would multiply the prompt, and every hop re-bills the
    whole conversation so far. ``spent_before`` is what this user had already
    spent today when the turn started: combined with the running cost it lets the
    loop stop escalating a turn that has crossed the daily cap, which the
    pre-turn check alone cannot do now that a turn is up to ``max_tool_hops + 1``
    calls rather than one.

    The hop cap does **not** end in a canned sentence. The last permitted call is
    made with no tools declared, so the model cannot ask for more and has to
    answer in prose from what it already gathered — a better message than any
    hardcoded one, and still "a plain message, not an error".
    """
    work = list(messages)
    max_hops = max(0, cfg.max_tool_hops)

    for hop in range(max_hops + 1):
        # Tools are withheld on the final iteration by construction, which is what
        # bounds the provider calls per question at max_hops + 1, hard.
        offer_tools = bool(tools) and hop < max_hops
        if offer_tools and cfg.daily_usd_limit > 0:
            running = estimate_cost(state.input_tokens, state.output_tokens,
                                    cfg.price_in_per_mtok, cfg.price_out_per_mtok)
            if spent_before + running >= cfg.daily_usd_limit:
                # Out of budget mid-turn: stop escalating, but let the model make
                # one last tools-free call so the user gets an answer rather than
                # a severed stream.
                offer_tools = False

        result = provider.StreamResult()
        for fragment in provider.stream(
            cfg, system, work, result,
            tools=tools if offer_tools else None,
        ):
            state.text += fragment
            yield "token", {"text": fragment}

        state.absorb(result)
        state.hops = hop + 1

        if not result.tool_calls:
            return
        if not offer_tools:
            # It asked for tools on a call where none were declared. Nothing to
            # run, and no hops left to run them in.
            state.capped = True
            state.text += CAP_NOTE
            yield "token", {"text": CAP_NOTE}
            return

        work.append(_assistant_turn(result))
        for call in result.tool_calls:
            yield from _run_one(call, work, state, dispatch=dispatch, budget=budget)

    # Unreachable: the final iteration has offer_tools False, so it either
    # returns on no tool calls or on the capped branch above.
    state.capped = True


def _run_one(call: provider.ToolCall, work: list[dict], state: TurnState, *,
             dispatch: Callable[[str, dict], str],
             budget: Budget) -> Iterator[tuple[str, dict]]:
    """Dispatch one tool call, append its result, and report both events.

    The ``role: "tool"`` message must follow its assistant message immediately
    and carry the matching ``tool_call_id``, one per call and in the same order,
    or the provider rejects the next request outright.
    """
    args = tools_module.parse_arguments(call.arguments)
    if args is None:
        content = "Error: arguments must be a JSON object."
        args = {}
    else:
        content = dispatch(call.name, args)

    yield "tool_call", {"name": call.name, "args": args}

    content = budget.take(content) or BUDGET_EXHAUSTED
    work.append({
        "role": "tool",
        "tool_call_id": call.id,
        "name": call.name,
        "content": content,
    })

    summary = _summarize(call.name, content)
    yield "tool_result", {"name": call.name, "summary": summary}
    state.tool_calls.append({
        "name": call.name,
        "args": args,
        "summary": summary,
        "chars": len(content),
    })
