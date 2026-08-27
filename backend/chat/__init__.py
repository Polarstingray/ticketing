"""The in-app chat assistant.

An assistant that answers questions about ticket context and resolver runs. It
reads only what the calling user may already read — ``context.py`` goes through
the same ``can_view_ticket`` gate every ticket route uses — and in this phase it
has no tools and performs no writes at all.

See ``docs/chat-design.md`` for the design and the build order. Module map:

===============  ==============================================================
``config.py``    Env-driven provider settings; the feature's on/off switch
``budget.py``    Character budget for the context pack, and cost estimation
``context.py``   Builds the permission-scoped context pack for a ticket
``prompts.py``   The system prompt
``provider.py``  One OpenAI-compatible chat completion
===============  ==============================================================
"""
