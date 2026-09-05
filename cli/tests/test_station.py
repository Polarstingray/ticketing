"""The station: inventory, identity discovery, and unit-state semantics.

Nothing here shells out to systemctl or touches the network. The parts that do
are thin wrappers around `subprocess`; what is worth testing is the logic that
decides *what* to ask systemd about and how to read the answer, which is where
every bug in this module has actually been.
"""
import pytest

from stingray_cli import cmd_station
from stingray_cli.config import ConfigError
from stingray_cli.station import identity, inventory, units
from stingray_cli.station.inventory import Resolver, Station


@pytest.fixture
def isolated_station(tmp_path, monkeypatch):
    path = tmp_path / "stations.toml"
    monkeypatch.setenv("STINGRAY_STATIONS", str(path))
    return path


def _resolver(handle="gemini", instance=None, profile="local", bot=3,
              prefix="stingray-resolver", checkout="/srv/ticketing"):
    from pathlib import Path
    instance = instance or handle
    return Resolver(handle=handle, instance=instance, profile=profile,
                    checkout=Path(checkout), env_file=f".env.{instance}",
                    bot_user_id=bot, unit_prefix=prefix)


# --- inventory --------------------------------------------------------------

def test_inventory_round_trips(isolated_station):
    station = Station(name="box", resolvers={"gemini": _resolver()})
    inventory.save_station(station)
    back = inventory.load_station()
    assert back.name == "box"
    assert back.resolvers["gemini"].bot_user_id == 3
    assert back.resolvers["gemini"].instance == "gemini"
    assert str(back.resolvers["gemini"].checkout) == "/srv/ticketing"


def test_inventory_is_not_world_readable(isolated_station):
    import stat
    inventory.save_station(Station(name="box", resolvers={}))
    assert stat.S_IMODE(isolated_station.stat().st_mode) == 0o600


def test_handle_and_instance_differ_for_a_repeated_name(isolated_station):
    """The same instance name against two servers is the case this host has."""
    station = Station(name="box", resolvers={
        "claude-lite": _resolver("claude-lite", profile="local", bot=5),
        "claude-lite@home": _resolver("claude-lite@home", instance="claude-lite",
                                      profile="home", bot=4,
                                      prefix="stingray-ubvm"),
    })
    inventory.save_station(station)
    back = inventory.load_station()
    assert back.resolvers["claude-lite@home"].instance == "claude-lite"
    # Both drive their own unit family, which is what keeps them apart.
    assert back.resolvers["claude-lite"].timer_unit == "stingray-resolver@claude-lite.timer"
    assert back.resolvers["claude-lite@home"].timer_unit == "stingray-ubvm@claude-lite.timer"


def test_get_prefers_a_handle_then_falls_back_to_an_instance():
    station = Station(name="box", resolvers={
        "claude-lite": _resolver("claude-lite", profile="local", bot=5),
        "claude-lite@home": _resolver("claude-lite@home", instance="claude-lite",
                                      profile="home", bot=4),
        "solo": _resolver("solo", bot=9),
    })
    assert station.get("claude-lite").profile == "local"   # exact handle wins
    assert station.get("solo").handle == "solo"


def test_get_refuses_an_ambiguous_instance_name():
    station = Station(name="box", resolvers={
        "a@one": _resolver("a@one", instance="a", profile="one", bot=1),
        "a@two": _resolver("a@two", instance="a", profile="two", bot=2),
    })
    with pytest.raises(ConfigError) as exc:
        station.get("a")
    assert "more than one" in str(exc.value)
    assert "a@one" in str(exc.value)


def test_by_bot_is_scoped_to_a_server():
    """Bot 5 is a different account on each server, so the id alone is no key."""
    station = Station(name="box", resolvers={
        "local5": _resolver("local5", profile="local", bot=5),
    })
    assert station.by_bot("local", 5).handle == "local5"
    assert station.by_bot("home", 5) is None


def test_dotted_names_are_refused():
    # A dotted key is a nested table in TOML: `[resolver.a.b]` would read back
    # as resolver "a" containing "b".
    with pytest.raises(ConfigError):
        inventory.validate_name("mistral-bot.bak")
    with pytest.raises(ConfigError):
        inventory.validate_name("has/slash")
    assert inventory.validate_name("claude-lite") == "claude-lite"


def test_a_handle_may_carry_an_at_sign_but_an_instance_may_not():
    assert inventory.validate_name("claude-lite@home", handle=True)
    with pytest.raises(ConfigError):
        inventory.validate_name("claude-lite@home")


def test_missing_inventory_explains_how_to_make_one(isolated_station):
    with pytest.raises(ConfigError) as exc:
        inventory.load_station()
    assert "stingray station init" in str(exc.value)
    assert inventory.load_station(required=False).resolvers == {}


# --- identity files ---------------------------------------------------------

@pytest.mark.parametrize("filename, expected", [
    (".env", True),
    (".env.gemini", True),
    (".env.claude-lite", True),
    (".env.example", False),
    (".env.mistral-bot.bak-critique-fix", False),   # a backup, not an identity
    (".env.alibaba-qwen-ubvm.save", False),
    ("env.gemini", False),
])
def test_identity_file_recognition(filename, expected):
    assert identity.is_identity_file(filename) is expected


def test_discover_skips_backups_and_the_template(tmp_path):
    for name in (".env", ".env.gemini", ".env.example", ".env.gemini.bak"):
        (tmp_path / name).write_text("STINGRAY_URL=x\n")
    found = [p.name for p in identity.discover(tmp_path)]
    assert found == [".env", ".env.gemini"]


def test_identity_name_matches_the_resolvers_own_rule():
    assert identity.identity_name(".env") == "default"
    assert identity.identity_name(".env.gemini") == "gemini"


def test_read_env_ignores_comments_and_strips_quotes(tmp_path):
    path = tmp_path / ".env.x"
    path.write_text('# a comment\nSTINGRAY_URL="http://x/api"\nEMPTY\nBOT=3\n')
    env = identity.read_env(path)
    assert env == {"STINGRAY_URL": "http://x/api", "BOT": "3"}


def test_secrets_are_shown_only_as_a_prefix():
    assert identity.redact("STINGRAY_API_KEY", "sk_abcdefghijklmnop") == "sk_abcdefgh…"
    assert identity.redact("RESOLVER_AGENT", "claude") == "claude"


# --- unit state -------------------------------------------------------------

def test_loaded_does_not_mean_installed_for_a_template_instance():
    """systemd reports LoadState=loaded for *any* instance of a live template.

    This is the trap that made an earlier version of `station init` adopt a
    stale identity over a running one: every conceivable instance looked real.
    """
    ghost = units.UnitState(name="stingray-ubvm@nope.timer", loaded=True,
                            active="inactive", enabled="disabled")
    real = units.UnitState(name="stingray-ubvm@mistral-bot.timer", loaded=True,
                           active="active", enabled="enabled")
    assert ghost.loaded and not ghost.installed
    assert real.installed and real.ok


def test_an_active_but_unenabled_instance_still_counts_as_installed():
    started = units.UnitState(name="x.service", loaded=True, active="active",
                              enabled="")
    assert started.installed


def test_render_renames_the_family_everywhere(tmp_path):
    template = (
        "WorkingDirectory=/opt/ticketing/resolver\n"
        "Wants=stingray-resolver@%i.timer\n"
        "ExecStart=/opt/ticketing/resolver/.venv/bin/python listen.py "
        "--unit stingray-resolver@%i.service\n"
    )
    out = units.render(template, tmp_path / "resolver", "stingray-ubvm")
    assert "/opt/ticketing/resolver" not in out
    assert "Wants=stingray-ubvm@%i.timer" in out
    # The listener must poke its *own* family, or it starts the other server's
    # sweep for an identity that shares an instance name.
    assert "--unit stingray-ubvm@%i.service" in out


def test_render_leaves_the_default_family_alone(tmp_path):
    out = units.render("Wants=stingray-resolver@%i.timer\n", tmp_path,
                       units.TEMPLATE_PREFIX)
    assert out == "Wants=stingray-resolver@%i.timer\n"


# --- profile matching -------------------------------------------------------

def test_a_url_that_matches_no_profile_never_falls_back(isolated_config,
                                                        monkeypatch):
    """Falling back would file a localhost resolver under another server."""
    from stingray_cli import config as cfgstore
    cfgstore.save_profile("home", {"url": "https://tickets.example/api",
                                   "api_key": "sk_x"})
    assert cmd_station._profile_for_url("https://tickets.example/api", None) == "home"
    assert cmd_station._profile_for_url("http://localhost:3000/api", "home") == ""
    # No URL at all is the only case a fallback is allowed to answer.
    assert cmd_station._profile_for_url("", "home") == "home"


def test_profile_match_ignores_a_trailing_slash(isolated_config):
    from stingray_cli import config as cfgstore
    cfgstore.save_profile("home", {"url": "https://tickets.example/api",
                                   "api_key": "sk_x"})
    assert cmd_station._profile_for_url("https://tickets.example/api/", None) == "home"
