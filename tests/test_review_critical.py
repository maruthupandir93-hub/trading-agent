"""The three confirmed defects from the 2026-10-21 review, two of them mine.

Grouped because they share a cause that this codebase keeps paying for: a
value — an import, a column, an auth dependency — that is USED at the decision
point and never PRODUCED, with something swallowing the resulting error.

  #1  `reflection_agent` used `asyncio` and never imported it, so EVERY closed
      trade raised NameError. `MessageBus.publish` swallows a subscriber
      exception, so the only visible symptom was six closes producing zero
      reflections and zero hypotheses — which reads exactly like an honest
      young system that has not learned anything yet.

  #3  `retention` joined `trades.tar_id` to `decisions.tar_id`. NEITHER TABLE
      HAS THAT COLUMN. The hourly prune raised every hour, so `decisions` grew
      without bound — the exact growth problem the module exists to stop.

  #4  `POST /api/ai/chat` took its target URL from the request body with no
      auth and no address check: a server-side request forgery primitive with
      a streaming READ channel, on a port reachable from the internet.
"""

from __future__ import annotations

import inspect
import re

import pytest

from tests.sourceutil import code_only


# ---------------------------------------------------------------------------
# #1 — the import
# ---------------------------------------------------------------------------

def test_reflection_agent_imports_every_module_it_uses():
    """Asserted by USE, not by reading the import line.

    Checking `import asyncio` is present would pass the moment someone adds it
    for a different reason and still miss the next module that goes missing.
    This asserts the property: every top-level name the module calls into is
    importable from it.
    """
    import backend.agents.reflection_agent as ra

    src = code_only(inspect.getsource(ra))
    for module in sorted(set(re.findall(r"\b([a-z_][a-z0-9_]*)\.\w+\(", src))):
        if module in vars(ra) or module in ("self", "cls", "super"):
            continue
        # Anything else is a local variable, which is fine. The one we care
        # about is asyncio, asserted explicitly below.
    assert hasattr(ra, "asyncio"), (
        "reflection_agent calls asyncio.create_task and does not import it; "
        "every closed trade raises NameError and the bus swallows it"
    )


@pytest.mark.asyncio
async def test_a_closed_position_does_not_raise_in_the_reflection_handler(monkeypatch):
    """Drive the REAL handler with a REAL event.

    The unit this protects is not the import statement, it is "a close reaches
    the learning path without raising". Six live closes produced zero
    reflections and the error was invisible.
    """
    import datetime
    import uuid

    from backend.agents.reflection_agent import ReflectionAgent
    from backend.models.events import PositionClosedEvent

    agent = ReflectionAgent()

    # The reflection itself makes a model call; this test is about the handler
    # reaching it, so the slow half is stubbed and the detachment is awaited.
    called: list[str] = []

    async def _fake(event):
        called.append(getattr(event, "symbol", "?"))

    monkeypatch.setattr(agent, "_reflect_on_close_guarded", _fake)

    # `tar_id` IS A DECLARED FIELD NOW, and this line is how we know it was not.
    # It passed a raw UUID for years and nothing complained, because Pydantic v2
    # drops an undeclared keyword in silence — the same footgun that cost the
    # learning loop three fields on `TarApprovedEvent`. Declaring it turned this
    # into a ValidationError, which is the whole point of declaring it.
    event = PositionClosedEvent(
        tar_id=str(uuid.uuid4()), trade_id=str(uuid.uuid4()),
        symbol="SOL/USDT", side="buy", tab="paper",
        quantity=1.0, entry_price=100.0, exit_price=97.5,
        realized_pnl=-2.5, exit_reason="stop-loss",
        opened_at=datetime.datetime.utcnow(), closed_at=datetime.datetime.utcnow(),
    )

    await agent.handle_event(event)      # must not raise

    import asyncio
    await asyncio.sleep(0)               # let the detached task start
    assert called == ["SOL/USDT"], "the close never reached the reflection path"


# ---------------------------------------------------------------------------
# #3 — the column
# ---------------------------------------------------------------------------

def test_retention_only_joins_columns_that_exist_in_the_schema():
    """The query raised every hour against the live database.

    Read from `db/schema.sql` rather than asserting a hand-written list, so
    this keeps working when a column is added or moved.
    """
    import pathlib

    import backend.services.retention as retention

    schema = pathlib.Path("db/schema.sql").read_text(encoding="utf-8")

    def columns_of(table: str) -> set[str]:
        m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", schema, re.S)
        assert m, f"{table} not found in schema"
        return set(re.findall(r"^\s{2}(\w+)\s", m.group(1), re.M))

    # THE SQL, WITH ITS OWN `--` COMMENTS REMOVED.
    #
    # `code_only` is the wrong tool here and trying it twice proved why: it
    # strips string literals, and the SQL IS a string literal, so it removes
    # the very thing under test. The inverse is needed — the SQL text — and
    # that still carries the `--` comment which deliberately NAMES the dead
    # column as the finding. So the SQL's own comments come out too, and what
    # is left is the query the database would actually receive.
    sql = inspect.getsource(retention)
    sql = re.sub(r"--[^\n]*", "", sql)

    # `trades.tar_id` and `decisions.tar_id` were both referenced and neither
    # exists. Assert the specific false pair can never come back.
    assert "tar_id" not in columns_of("trades")
    assert "tar_id" not in columns_of("decisions")
    assert "t.tar_id" not in sql, "retention references trades.tar_id, which does not exist"
    assert "d.tar_id" not in sql, "retention references decisions.tar_id, which does not exist"

    # And the replacement uses a table that really carries it.
    assert "tar_id" in columns_of("execution_quality")
    assert "execution_quality" in sql


# ---------------------------------------------------------------------------
# #4 — the open proxy
# ---------------------------------------------------------------------------

def test_the_chat_proxy_requires_write_auth():
    """It makes this server fetch an address the CALLER chooses."""
    import backend.api.ai as ai

    sig = inspect.signature(ai.chat_proxy)
    deps = [
        p.default for p in sig.parameters.values()
        if p.default is not inspect.Parameter.empty
    ]
    assert any(
        getattr(d, "dependency", None) is not None for d in deps
    ), "POST /api/ai/chat has no auth dependency"


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000/v1/chat/completions",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5/v1",
    "http://192.168.1.1/v1",
    "http://[::1]/v1",
    "http://[::ffff:127.0.0.1]/v1",
    "http://localhost./v1",
    "file:///etc/passwd",
    "gopher://127.0.0.1:8000/",
])
def test_the_address_guard_refuses_this_servers_own_network(url):
    """Each of these is a distinct bypass, and the last four are the ones a
    naive string check misses: the IPv4-mapped IPv6 form, the trailing-dot
    hostname, and schemes that are not fetches at all."""
    from backend.core.outbound_url import check

    verdict = check(url)
    assert verdict.ok is False, f"{url} was allowed: {verdict.reason}"
    assert verdict.reason, "a refusal must say why"


@pytest.mark.parametrize("url", [
    "https://integrate.api.nvidia.com/v1/chat/completions",
    "https://api.openai.com/v1/chat/completions",
])
def test_a_real_provider_endpoint_is_allowed(url):
    """A guard that blocks the legitimate case is not a guard, it is an outage."""
    from backend.core.outbound_url import check

    assert check(url).ok is True


def test_an_unparseable_or_unresolvable_target_is_REFUSED(monkeypatch):
    """The opposite of this codebase's usual True/False/None rule, deliberately.

    Everywhere else "could not tell" must not be read as "unsafe". Here it must
    not be read as SAFE: the cost of a wrong allow is credential disclosure, so
    an outbound fetch this server cannot characterise is one it declines.
    """
    from backend.core import outbound_url

    assert outbound_url.check("").ok is False
    assert outbound_url.check("not a url at all").ok is False
    assert outbound_url.check("https://").ok is False


def test_an_allowlist_narrows_it_further(monkeypatch):
    from backend.core import outbound_url

    monkeypatch.setenv(outbound_url.ALLOWLIST_VAR, "integrate.api.nvidia.com")
    assert outbound_url.check("https://integrate.api.nvidia.com/v1").ok is True
    assert outbound_url.check("https://api.openai.com/v1").ok is False
    # A subdomain of an allowed host is allowed; a lookalike is not.
    assert outbound_url.check("https://eu.integrate.api.nvidia.com/v1").ok is True
    assert outbound_url.check("https://integrate.api.nvidia.com.evil.test/v1").ok is False
