"""Position persistence — the watch list and the backend paper book.

WHAT WAS BROKEN
---------------
`PositionMonitorAgent._open` was a dict on the instance and
`services/portfolio_store._portfolio` was a module-level dict. Neither was
written anywhere, so a restart forgot every open position and reset paper cash
to its starting figure. Spec Section 22.8: *"the worst case is not 'the bot makes
a bad trade' but 'the bot goes silent while holding a leveraged position'."*

WHAT THESE TESTS DO AND DO NOT COVER
------------------------------------
There is no Postgres in this suite, so `get_db_pool()` returns None everywhere.
That is not a limitation to work around — it is the DEGRADED PATH, and it is the
path this code runs on for anyone without `DATABASE_URL`, so it gets tested
directly: every store function must return a falsy value, log, and never raise.

For the durable path the pool is faked. The fake records SQL rather than
executing it, so what is under test is *this code's* contract with asyncpg — the
statements it issues, the order, and whether they are inside a transaction — not
asyncpg itself. A fake that pretended to be a SQL engine would be testing the
fake.
"""

import asyncio
import datetime
import uuid
from decimal import Decimal

import pytest

from backend.core.message_bus import MessageBus
from backend.models.events import OrderFilledEvent, TarApprovedEvent, TickReceivedEvent


# ---------------------------------------------------------------------------
# A fake asyncpg pool
# ---------------------------------------------------------------------------


class FakeConnection:
    """Records statements. `fetch`/`fetchrow` answer from canned rows.

    Deliberately does NOT interpret SQL. The assertions below are about which
    statements this code issues and in what order, which is exactly what broke
    when a delete committed without its inserts.
    """

    def __init__(self, rows=None, row=None, fail_on=None):
        self.statements = []
        self.transaction_depth = 0
        self.in_transaction_at = []
        self._rows = rows if rows is not None else []
        self._row = row
        self._fail_on = fail_on

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self):
                conn.transaction_depth += 1
                return None

            async def __aexit__(self, *exc):
                conn.transaction_depth -= 1
                return False

        return _Tx()

    async def execute(self, sql, *args):
        if self._fail_on and self._fail_on in sql:
            raise RuntimeError("simulated database failure")
        self.statements.append(sql.strip())
        # Records whether each statement ran inside a transaction, so the
        # all-or-nothing claim is verified rather than assumed.
        self.in_transaction_at.append(self.transaction_depth > 0)

    async def fetch(self, sql, *args):
        if self._fail_on and self._fail_on in sql:
            raise RuntimeError("simulated database failure")
        self.statements.append(sql.strip())
        return self._rows

    async def fetchrow(self, sql, *args):
        if self._fail_on and self._fail_on in sql:
            raise RuntimeError("simulated database failure")
        self.statements.append(sql.strip())
        return self._row


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acquire:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Acquire()


@pytest.fixture
def fake_pool(monkeypatch):
    """Install a fake pool into both store modules.

    Patched on each module rather than on `core.db`, because both import
    `get_db_pool` by name at module load — patching the source would leave the
    already-bound references pointing at the real one.
    """

    def _install(conn):
        from backend.services import portfolio_store, position_store

        pool = FakePool(conn)
        monkeypatch.setattr(position_store, "get_db_pool", lambda: pool)
        monkeypatch.setattr(portfolio_store, "get_db_pool", lambda: pool)
        return pool

    return _install


@pytest.fixture(autouse=True)
def reset_portfolio():
    """The backend book is module-level state; leaking it across tests would make
    one test's cash balance another test's starting condition."""
    from backend.services import portfolio_store

    original = portfolio_store._portfolio
    portfolio_store._portfolio = {
        "paper": {"cash": portfolio_store.PAPER_STARTING_CASH, "positions": []},
        "real": {"positions": []},
    }
    yield
    portfolio_store._portfolio = original


# ---------------------------------------------------------------------------
# The degraded path — no database at all
# ---------------------------------------------------------------------------


async def test_saving_without_a_database_reports_false_rather_than_raising():
    """A storage outage must not become a trading outage.

    `save_watch_list` returning False is the caller's signal that the position is
    watched in memory only. Raising here would abort `handle_event` and abandon
    the watch entirely — strictly worse than watching without durability.
    """
    from backend.services.position_store import save_watch_list

    assert await save_watch_list([{"tar_id": "t1", "status": "open"}]) is False


async def test_loading_without_a_database_returns_empty_not_an_error():
    from backend.services.position_store import load_watch_list

    assert await load_watch_list() == []


async def test_the_monitor_still_works_with_no_database(caplog):
    """The whole agent must behave exactly as it did before persistence existed."""
    from backend.agents.position_monitor import PositionMonitorAgent

    agent = PositionMonitorAgent()
    tar_id = uuid.uuid4()
    await agent.handle_event(
        TarApprovedEvent(
            tar_id=tar_id, symbol="BTC/USDT", direction="long", approved_size=0.05,
            approved_leverage=2, cro_rationale="ok", stop_loss=68_000.0,
            tab="paper", take_profit=76_000.0,
        )
    )
    await agent.handle_event(
        OrderFilledEvent(
            tar_id=tar_id, exchange="binance_futures", order_id="o1", symbol="BTC/USDT",
            side="buy", tab="paper", fill_price=70_000.0, fill_quantity=0.05,
            slippage_bps=1.0, fee=0.1,
        )
    )

    assert agent.open_position_count == 1
    assert await agent.restore() == 0  # nothing to read, and it says so rather than pretending


async def test_a_database_error_during_save_is_logged_not_raised(fake_pool, caplog):
    from backend.services.position_store import save_watch_list

    fake_pool(FakeConnection(fail_on="DELETE FROM monitored_positions"))

    with caplog.at_level("ERROR"):
        assert await save_watch_list([{"tar_id": "t1", "status": "open"}]) is False

    assert "still watched in this process" in caplog.text


# ---------------------------------------------------------------------------
# The durable path — watch list
# ---------------------------------------------------------------------------


async def test_the_watch_list_is_replaced_inside_one_transaction(fake_pool):
    """Delete-then-insert, all inside a transaction.

    A delete that committed without its inserts would leave an EMPTY watch list
    on disk while positions are open — the worst intermediate state, because the
    next restart reads it as "nothing to watch" and is confident about it.
    """
    from backend.services.position_store import save_watch_list

    conn = FakeConnection()
    fake_pool(conn)

    ok = await save_watch_list([
        {"tar_id": "t1", "status": "open", "symbol": "BTC/USDT", "tab": "paper",
         "side": "buy", "qty": 0.05, "entry_price": 70_000.0, "stop_loss": 68_000.0,
         "take_profit": 76_000.0, "peak_price": 70_500.0, "opened_at": None},
    ])

    assert ok is True
    assert conn.statements[0].startswith("DELETE FROM monitored_positions")
    assert "INSERT INTO monitored_positions" in conn.statements[1]
    assert all(conn.in_transaction_at), "every statement must be inside the transaction"


async def test_load_converts_numerics_to_float_and_keeps_none_as_none(fake_pool):
    """Postgres `numeric` arrives as Decimal, and a pending row has no entry price.

    Coercing that None to 0.0 would give a restored position an entry of zero,
    and every P&L figure derived from it would be wrong in a way that looks
    precise — the same class of bug as `prob_of_ruin: 0.0` with no data.
    """
    from backend.services.position_store import load_watch_list

    fake_pool(FakeConnection(rows=[{
        "tar_id": "t1", "status": "pending", "symbol": "BTC/USDT", "tab": "paper",
        "side": None, "qty": None, "entry_price": None,
        "stop_loss": Decimal("68000.5"), "take_profit": None,
        "peak_price": None, "opened_at": None,
    }]))

    rows = await load_watch_list()
    assert len(rows) == 1
    assert rows[0]["stop_loss"] == pytest.approx(68_000.5)
    assert isinstance(rows[0]["stop_loss"], float)
    assert rows[0]["entry_price"] is None
    assert rows[0]["qty"] is None


# ---------------------------------------------------------------------------
# What restore actually buys — the safety property, not just the round trip
# ---------------------------------------------------------------------------


def _stored_open_row(stop=68_000.0, side="buy", peak=None):
    # `opened_at` is NAIVE here because that is what `load_watch_list` returns —
    # see `_as_naive_utc` and the aware-timestamp test below for why the
    # conversion exists and what it prevents.
    return {
        "tar_id": "tar-restored", "status": "open", "symbol": "BTC/USDT", "tab": "paper",
        "side": side, "qty": 0.05, "entry_price": 70_000.0, "stop_loss": stop,
        "take_profit": 76_000.0, "peak_price": peak,
        "opened_at": datetime.datetime.utcnow(),
    }


class _RecordingExecution:
    def __init__(self, fill_price=67_450.0):
        self.closes = []
        self._fill_price = fill_price

    async def close_position(self, **kwargs):
        self.closes.append(kwargs)
        return self._fill_price


async def test_a_restored_position_is_closed_by_the_next_tick_through_its_stop(monkeypatch):
    """THE POINT OF ALL OF THIS.

    Persisting a row is worthless if the restored position is not actually
    enforced. Before this, a restart left a real position open at the exchange
    with its stop living only in a dead process's memory.
    """
    from backend.agents.position_monitor import PositionMonitorAgent
    from backend.services import position_store

    async def fake_load():
        return [_stored_open_row()]

    async def fake_save(rows):
        return True

    monkeypatch.setattr(position_store, "load_watch_list", fake_load)
    monkeypatch.setattr(position_store, "save_watch_list", fake_save)

    execution = _RecordingExecution()
    agent = PositionMonitorAgent()
    agent.rebind_bus(MessageBus())
    agent.attach_execution(execution)

    assert await agent.restore() == 1

    # Above the stop: nothing happens.
    await agent.handle_event(
        TickReceivedEvent(symbol="BTC/USDT", price=69_000.0, volume=1.0, exchange="binance_futures")
    )
    assert agent.open_position_count == 1

    # Through the stop: the restored position closes on its own.
    await agent.handle_event(
        TickReceivedEvent(symbol="BTC/USDT", price=67_500.0, volume=1.0, exchange="binance_futures")
    )
    assert agent.open_position_count == 0
    assert len(execution.closes) == 1
    assert execution.closes[0]["reason"] == "stop-loss"


async def test_a_pending_approval_survives_so_the_fill_is_not_unprotected(monkeypatch, caplog):
    """A restart between TAR_APPROVED and ORDER_FILLED must not orphan the fill.

    Without the pending row the approved stop is gone, `_register_fill` finds no
    match, and a genuinely monitorable position is logged as UNPROTECTED and left
    unwatched — purely because of the restart's timing.
    """
    from backend.agents.position_monitor import PositionMonitorAgent
    from backend.services import position_store

    tar_id = uuid.uuid4()

    async def fake_load():
        return [{
            "tar_id": str(tar_id), "status": "pending", "symbol": "BTC/USDT", "tab": "paper",
            "side": None, "qty": None, "entry_price": None, "stop_loss": 68_000.0,
            "take_profit": 76_000.0, "peak_price": None, "opened_at": None,
        }]

    async def fake_save(rows):
        return True

    monkeypatch.setattr(position_store, "load_watch_list", fake_load)
    monkeypatch.setattr(position_store, "save_watch_list", fake_save)

    agent = PositionMonitorAgent()
    agent.rebind_bus(MessageBus())
    await agent.restore()

    with caplog.at_level("CRITICAL"):
        await agent.handle_event(
            OrderFilledEvent(
                tar_id=tar_id, exchange="binance_futures", order_id="o9", symbol="BTC/USDT",
                side="buy", tab="paper", fill_price=70_000.0, fill_quantity=0.05,
                slippage_bps=1.0, fee=0.1,
            )
        )

    assert "UNPROTECTED POSITION" not in caplog.text
    assert agent.open_position_count == 1
    assert float(agent.snapshot_open()[0]["stopLoss"]) == pytest.approx(68_000.0)


async def test_an_aware_stored_timestamp_does_not_break_the_close(fake_pool, monkeypatch):
    """REGRESSION. Found while writing these tests, and it was not a test artifact.

    `monitored_positions.opened_at` is `timestamptz`, so asyncpg hands back an
    AWARE datetime, while everything the agent builds in-process uses naive
    `utcnow()`. `_close` computes `held = utcnow() - pos.opened_at`, which raises
    TypeError on the mix — AFTER the exchange has already filled the closing
    order.

    So the un-fixed behaviour was: a restored position stops out, the close
    really executes, then the agent raises before removing it from the watch list
    or publishing POSITION_CLOSED. Every following tick closed the same flat
    position again.

    Deliberately routed through the REAL `load_watch_list`, so the conversion
    seam itself is what is under test rather than a hand-built row.
    """
    from backend.agents.position_monitor import PositionMonitorAgent
    from backend.services import position_store

    aware = datetime.datetime.now(datetime.timezone.utc)
    fake_pool(FakeConnection(rows=[{
        "tar_id": "tar-tz", "status": "open", "symbol": "BTC/USDT", "tab": "paper",
        "side": "buy", "qty": Decimal("0.05"), "entry_price": Decimal("70000"),
        "stop_loss": Decimal("68000"), "take_profit": Decimal("76000"),
        "peak_price": Decimal("70000"), "opened_at": aware,
    }]))

    rows = await position_store.load_watch_list()
    assert rows[0]["opened_at"].tzinfo is None, "the storage boundary must hand back naive UTC"

    monkeypatch.setattr(position_store, "save_watch_list",
                        lambda r: asyncio.sleep(0, result=True))

    execution = _RecordingExecution()
    agent = PositionMonitorAgent()
    agent.rebind_bus(MessageBus())
    agent.attach_execution(execution)
    await agent.restore()

    # This is the line that used to raise.
    await agent.handle_event(
        TickReceivedEvent(symbol="BTC/USDT", price=67_500.0, volume=1.0, exchange="binance_futures")
    )

    assert agent.open_position_count == 0, "the position must actually leave the watch list"
    assert len(execution.closes) == 1, "and must be closed exactly once, not once per tick"


async def test_restore_falls_back_to_the_entry_price_for_a_missing_peak(monkeypatch):
    """`peak_price` is not written on every tick, so it can come back NULL.

    It feeds `tighten_stop`'s would-fire-immediately guard. None there would
    disable that guard on every restored position, so it falls back to the entry
    — the one price guaranteed to have been reached.
    """
    from backend.agents.position_monitor import PositionMonitorAgent
    from backend.services import position_store

    monkeypatch.setattr(position_store, "load_watch_list",
                        lambda: asyncio.sleep(0, result=[_stored_open_row(peak=None)]))
    monkeypatch.setattr(position_store, "save_watch_list",
                        lambda rows: asyncio.sleep(0, result=True))

    agent = PositionMonitorAgent()
    agent.rebind_bus(MessageBus())
    await agent.restore()

    assert agent.snapshot_open()[0]["peakPrice"] == pytest.approx(70_000.0)


async def test_restore_does_not_clobber_a_position_already_tracked(monkeypatch):
    """Restore must be safe on a live agent — a stale snapshot must not win."""
    from backend.agents.position_monitor import PositionMonitorAgent
    from backend.services import position_store

    monkeypatch.setattr(position_store, "load_watch_list",
                        lambda: asyncio.sleep(0, result=[_stored_open_row(stop=60_000.0)]))
    monkeypatch.setattr(position_store, "save_watch_list",
                        lambda rows: asyncio.sleep(0, result=True))

    agent = PositionMonitorAgent()
    agent.rebind_bus(MessageBus())
    await agent.restore()
    # A live tighten happens after the restore.
    applied, _ = agent.tighten_stop("tar-restored", 65_000.0)
    assert applied

    # A second restore (a retry, a double-wire) must not revert it.
    await agent.restore()
    assert float(agent.snapshot_open()[0]["stopLoss"]) == pytest.approx(65_000.0)


async def test_a_close_is_persisted_before_position_closed_is_published(monkeypatch):
    """Ordering matters: a crash between the two would resume monitoring a
    position that is already flat, and close it a second time."""
    from backend.agents.position_monitor import PositionMonitorAgent
    from backend.services import position_store

    order = []

    async def fake_save(rows):
        order.append(("saved", len(rows)))
        return True

    monkeypatch.setattr(position_store, "save_watch_list", fake_save)
    monkeypatch.setattr(position_store, "load_watch_list",
                        lambda: asyncio.sleep(0, result=[_stored_open_row()]))

    bus = MessageBus()
    bus.subscribe("POSITION_CLOSED", lambda e: order.append(("published", 0)))

    agent = PositionMonitorAgent()
    agent.rebind_bus(bus)
    agent.attach_execution(_RecordingExecution())
    await agent.restore()

    await agent.handle_event(
        TickReceivedEvent(symbol="BTC/USDT", price=67_500.0, volume=1.0, exchange="binance_futures")
    )

    kinds = [k for k, _ in order]
    assert "saved" in kinds and "published" in kinds
    assert kinds.index("saved") < kinds.index("published")
    # The save that preceded the publish must have written an EMPTY list — the
    # position is gone, and a row left behind is what causes the double close.
    saved_before_publish = [n for k, n in order[: kinds.index("published")] if k == "saved"]
    assert saved_before_publish[-1] == 0


# ---------------------------------------------------------------------------
# The backend paper book
# ---------------------------------------------------------------------------


async def test_the_paper_book_is_restored_from_storage(fake_pool):
    from backend.services import portfolio_store

    fake_pool(FakeConnection(
        row={"cash": Decimal("18000.25")},
        rows=[{"tab": "paper", "symbol": "BTC/USDT", "qty": Decimal("0.1"),
               "avg_cost": Decimal("70000"), "margin_locked": Decimal("3500")}],
    ))

    assert await portfolio_store.load_portfolio() is True

    book = await portfolio_store.get_portfolio()
    assert book["paper"]["cash"] == pytest.approx(18_000.25)
    assert book["paper"]["positions"][0]["symbol"] == "BTC/USDT"
    # marginLocked is NOT qty*avgCost once leverage is involved; losing it would
    # make a restart over-report free cash.
    assert book["paper"]["positions"][0]["marginLocked"] == pytest.approx(3_500.0)


async def test_an_absent_stored_book_is_not_treated_as_an_empty_one(fake_pool):
    """No row means "this process has never traded", which is why
    `agent_paper_account` carries no seed — a seeded 25,000 would be
    indistinguishable from an account that traded its way back to exactly that."""
    from backend.services import portfolio_store

    fake_pool(FakeConnection(row=None, rows=[]))

    assert await portfolio_store.load_portfolio() is False
    book = await portfolio_store.get_portfolio()
    assert book["paper"]["cash"] == pytest.approx(portfolio_store.PAPER_STARTING_CASH)


async def test_a_paper_buy_is_written_to_the_agent_tables_not_the_browser_ones(fake_pool):
    """The table choice is the whole design decision — assert it, don't comment it.

    `lib/portfolioStore.server.ts::saveBook` runs `DELETE FROM positions` and
    replaces the browser's whole book. A backend writer there would have the
    operator's next save delete every position the agent holds.
    """
    from backend.services import portfolio_store

    conn = FakeConnection()
    fake_pool(conn)

    assert await portfolio_store.buy_paper("BTC/USDT", 0.1, 70_000.0, leverage=2.0) is True

    written = " ".join(conn.statements)
    assert "agent_paper_account" in written
    assert "agent_positions" in written
    assert "INSERT INTO positions" not in written
    assert "DELETE FROM positions" not in written
    assert "INSERT INTO paper_account" not in written
    assert all(conn.in_transaction_at)


async def test_a_failed_persist_does_not_fail_the_trade(fake_pool, caplog):
    """Reporting a successful buy as failed because storage is down would leave
    the caller believing it holds nothing while it holds something."""
    from backend.services import portfolio_store

    fake_pool(FakeConnection(fail_on="agent_paper_account"))

    with caplog.at_level("ERROR"):
        ok = await portfolio_store.buy_paper("BTC/USDT", 0.1, 70_000.0)

    assert ok is True
    book = await portfolio_store.get_portfolio()
    assert len(book["paper"]["positions"]) == 1
    assert "restart will reset it" in caplog.text
