"""Bounding what goes to the model, and pricing what came back.

Two small concerns that would otherwise be guessed at each call site:

* **The context budget.** A ticket with fifty comments and a 3,000-line code
  block must degrade predictably instead of failing at the provider with an
  opaque context-length error. ``Budget`` hands out a character allowance that
  sections draw from in priority order, so the header and description survive and
  the tail of the activity trail is what gets dropped.
* **Cost.** Priced from the operator's configured per-1M-token rates rather than
  a table of vendor prices baked into the app, which would go stale silently.
"""
from dataclasses import dataclass, field

# Appended wherever text was cut, so a truncated section is visibly truncated —
# both to the reader and to the model, which should not treat a cut-off comment
# thread as complete.
TRUNCATION_NOTE = "\n\n… [truncated to fit the context budget]"


def clip(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters, marked when anything was removed.

    The return value never exceeds ``limit``: the truncation note is paid for out
    of the limit, not added on top of it, so a pack made of clipped sections does
    not drift over budget by one note per section.

    Cuts at the last newline before the limit when there is one reasonably close,
    so a truncated code block or comment ends at a line boundary instead of
    mid-token. A non-positive limit yields the empty string. An allowance too
    small to hold the note *and* any content spends all of it on content: a
    section that is nothing but a truncation marker says nothing.
    """
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= len(TRUNCATION_NOTE):
        return text[:limit]
    room = limit - len(TRUNCATION_NOTE)
    cut = text[:room]
    nl = cut.rfind("\n")
    # Only honor the line boundary if it isn't throwing away most of the room we
    # have — strictly more than half of it must survive the retreat to the newline.
    if nl > room // 2:
        cut = cut[:nl]
    return cut.rstrip() + TRUNCATION_NOTE


@dataclass
class Budget:
    """A shrinking character allowance shared by the sections of a context pack.

    Sections are added in priority order; each one takes what it needs from the
    remaining allowance and is clipped when the allowance runs short. Once the
    allowance is exhausted, later sections are dropped entirely (``take`` returns
    the empty string), which is why ``context.py`` adds the header first and the
    activity trail last.
    """

    limit: int
    used: int = field(default=0, init=False)

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def take(self, text: str, *, cap: int | None = None) -> str:
        """Charge ``text`` against the allowance, clipping it to fit.

        ``cap`` additionally bounds this one section, so a single enormous code
        block can't consume the whole pack and starve the comments after it.
        """
        allowance = self.remaining
        if cap is not None:
            allowance = min(allowance, cap)
        out = clip(text, allowance)
        self.used += len(out)
        return out


def estimate_cost(input_tokens: int, output_tokens: int,
                  price_in_per_mtok: float, price_out_per_mtok: float) -> float:
    """USD for one completion, from per-1M-token rates. Unpriced ⇒ 0.0.

    Rounded to 6 decimals: a single cheap turn can cost well under a hundredth of
    a cent, and rounding to cents would sum a day of chat to zero.
    """
    cost = (input_tokens / 1_000_000) * price_in_per_mtok
    cost += (output_tokens / 1_000_000) * price_out_per_mtok
    return round(cost, 6)
