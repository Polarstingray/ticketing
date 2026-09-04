"""Validation for user-supplied webhook URLs — the SSRF boundary.

A webhook is an *outbound request the server makes to an address the user
chose*, which is the textbook SSRF setup: without a check, "https://10.0.0.5/x"
or "http://169.254.169.254/latest/meta-data/" turns this app into a proxy for
the private network and the cloud metadata service.

Two call sites share this module deliberately, so the two checks can never
drift:

* **creation/update** (``routers.webhooks``) — reject a bad URL with 422 before
  it is ever stored;
* **delivery** (the delivery worker, shipping separately) — call
  :func:`validate_webhook_url` *again* immediately before the request. DNS is
  mutable: a hostname that resolved to a public address at creation time can
  resolve to 127.0.0.1 a minute later (DNS rebinding). A delivery-time failure
  is not a validation error for the user — the worker records the delivery as
  ``DeliveryState.skipped`` with the reason.

Note the residual race this cannot close: the resolution done here and the one
the HTTP client does when it connects are two separate lookups. Closing that
fully means resolving once and connecting to the pinned IP with the hostname in
the ``Host`` header, which belongs to the delivery ticket that owns the socket.
"""
import ipaddress
import os
import socket
from urllib.parse import urlsplit

# Message prefix every rejection carries, so callers (and tests) can recognize
# an SSRF refusal without matching the whole sentence.
SSRF_DENY_REASON = "webhook URL rejected"

MAX_URL_LENGTH = 2000

# Ports a legitimate webhook receiver listens on. Anything else (22, 25, 6379,
# 3306, …) is a service that has no business receiving a POST from us, and
# reaching them is half the value of an SSRF.
ALLOWED_PORTS = {80, 443, 8080, 8443}

# Hostname suffixes that name internal infrastructure by convention rather than
# by address; these resolve to public-looking addresses in some environments, so
# the IP checks below would not catch them.
DENIED_HOST_SUFFIXES = (
    "metadata.google.internal",
    ".internal",
    ".local",
    ".localhost",
    "localhost",
)


def _deny(reason: str) -> "ValueError":
    return ValueError(f"{SSRF_DENY_REASON}: {reason}")


def _allow_insecure() -> bool:
    """Whether plain ``http://`` is permitted (off by default).

    Read per-call rather than at import so a test (or an operator running behind
    a trusted proxy) can flip it. It relaxes *only* the scheme — every address
    check below still applies, so this is not a way to reach localhost.
    """
    return os.environ.get("ALLOW_INSECURE_WEBHOOKS", "") == "1"


def _check_ip(value: str, *, literal: bool) -> None:
    """Raise if ``value`` is an address we must never connect to."""
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return  # not an address; hostname rules apply instead
    what = "host" if literal else f"host resolves to {value}"
    if ip.is_loopback:
        raise _deny(f"{what} is a loopback address")
    if ip.is_link_local:
        # Covers the cloud metadata endpoints: 169.254.169.254 (AWS/GCP/Azure)
        # and fd00:ec2::254 is caught by is_private below.
        raise _deny(f"{what} is a link-local address (cloud metadata range)")
    if ip.is_private:
        # is_private is true for RFC1918, unique-local (fc00::/7), and the
        # IPv4-mapped forms of both.
        raise _deny(f"{what} is a private address")
    if ip.is_multicast:
        raise _deny(f"{what} is a multicast address")
    if ip.is_reserved:
        raise _deny(f"{what} is a reserved address")
    if ip.is_unspecified:
        raise _deny(f"{what} is the unspecified address")


def resolve_and_check(host: str, *, allowed_hosts: frozenset[str] = frozenset()) -> list[str]:
    """Resolve ``host`` (A and AAAA) and reject if *any* answer is non-public.

    Returns the resolved addresses. Every answer must pass, not just the first:
    an attacker controls their own DNS zone and can return one public address
    beside a private one, so checking only ``[0]`` is checking nothing.

    A DNS failure raises — the caller decides what that means (422 at creation,
    a ``skipped`` delivery at send time).

    ``allowed_hosts`` (from the admin security-settings exemption list, see
    routers.security_settings) skips the address check entirely for an exact
    hostname match — an admin-authorized escape hatch for a webhook receiver
    that legitimately resolves to a private/internal address. It does NOT
    skip DNS resolution (the host must still resolve).
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise _deny(f"host {host!r} does not resolve ({exc})") from exc
    if not infos:
        raise _deny(f"host {host!r} does not resolve")

    addresses: list[str] = []
    for info in infos:
        address = info[4][0]
        if address not in addresses:
            addresses.append(address)
    if host in allowed_hosts:
        return addresses
    for address in addresses:
        _check_ip(address, literal=False)
    return addresses


def validate_webhook_url(
    url: str, *, allowed_hosts: frozenset[str] = frozenset(), allow_insecure: bool = False,
) -> str:
    """Return ``url`` unchanged if it is a safe outbound target, else raise.

    Raises ``ValueError`` (message prefixed with :data:`SSRF_DENY_REASON`) for a
    malformed URL, a disallowed scheme/port/host, or a host that resolves to any
    non-public address.

    ``allowed_hosts`` and ``allow_insecure`` come from the admin security
    settings (routers.security_settings.get_security_settings) — both callers
    (routers.webhooks and the delivery worker) read the current settings and
    pass them through here rather than this module reaching into the DB
    itself, keeping this module's only DB dependency at its call sites.
    ``allowed_hosts`` exempts an exact hostname from the private-address
    check only; scheme, port, userinfo, fragment, and the denied-suffix check
    still apply unconditionally. ``allow_insecure`` is OR'd with the
    ``ALLOW_INSECURE_WEBHOOKS`` env var (either can turn plain http on).
    """
    if not isinstance(url, str):
        raise _deny("url must be a string")
    url = url.strip()
    if not url:
        raise _deny("url must not be empty")
    if len(url) > MAX_URL_LENGTH:
        raise _deny(f"url too long (max {MAX_URL_LENGTH} chars)")
    if any(ord(c) < 32 or ord(c) == 127 for c in url):
        raise _deny("url must not contain control characters")

    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise _deny(f"url is not parseable ({exc})") from exc

    scheme = parts.scheme.lower()
    if scheme == "http":
        if not (allow_insecure or _allow_insecure()):
            raise _deny("scheme must be https (set ALLOW_INSECURE_WEBHOOKS=1 to allow http)")
    elif scheme != "https":
        raise _deny(f"scheme {parts.scheme!r} is not allowed; use https")

    if parts.username or parts.password:
        # Credentials in the URL would be sent to whatever the host turns out to
        # be, and are a classic way to disguise the real host from a reader.
        raise _deny("url must not contain userinfo (user:pass@)")
    if parts.fragment:
        raise _deny("url must not contain a fragment")

    try:
        hostname = parts.hostname
    except ValueError as exc:
        raise _deny(f"url has an invalid host ({exc})") from exc
    if not hostname:
        raise _deny("url must have a host")
    hostname = hostname.lower()

    try:
        port = parts.port
    except ValueError as exc:
        raise _deny(f"url has an invalid port ({exc})") from exc
    if port is not None and port not in ALLOWED_PORTS:
        raise _deny(f"port {port} is not allowed (allowed: {sorted(ALLOWED_PORTS)})")

    for suffix in DENIED_HOST_SUFFIXES:
        if hostname == suffix or hostname.endswith(suffix):
            raise _deny(f"host {hostname!r} names internal infrastructure")

    # An IP literal skips DNS entirely, so check it directly; anything else has
    # to be resolved before we can know what it points at.
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        resolve_and_check(hostname, allowed_hosts=allowed_hosts)
    else:
        if hostname not in allowed_hosts:
            _check_ip(hostname, literal=True)

    return url
