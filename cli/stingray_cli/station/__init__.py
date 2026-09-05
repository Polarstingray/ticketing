"""The station: the resolvers running on *this* host.

A resolver identity is four things that have to agree — a bot user on a Stingray
server, an API key for it, an ``.env.<name>`` in a resolver checkout, and a pair
of systemd units. Nothing forced them to agree before this package existed, and
the ways they drift are quiet ones: a bot id claimed twice, a unit pointing at
the wrong checkout, a listener that has never connected.

The inventory records *intent* (which identities this host means to run) and
everything else is derived live from systemd, git, the log files and the server,
so a stale inventory can never make the host behave differently — only make this
tool describe it wrongly, which ``doctor`` is there to catch.
"""
