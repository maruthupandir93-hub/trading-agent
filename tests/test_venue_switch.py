"""Switching the exchange the agent trades on — and when it must refuse.

WHY THE REFUSAL IS THE IMPORTANT TEST
=====================================
Binance and Bybit are DIFFERENT ACCOUNTS holding DIFFERENT MONEY. A position
opened on one does not exist on the other. Switching underneath an open real
position would leave it at the old venue while:

  * `PositionMonitorAgent` goes on enforcing its stop by placing orders on the
    NEW venue, where the position does not exist;
  * the resting stop this process left behind stays live and cannot be cancelled
    through the new client;
  * reconciliation compares the local book against the wrong exchange and reports
    every real position as a phantom.

None of those announce themselves. The switch would look like it worked.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from backend.api import admin
from backend.agents.position_monitor import get_position_monitor, reset_position_monitor
from backend.services import venue as venue_mod


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    reset_position_monitor()
    venue_mod.reset_venue()
    monkeypatch.setenv("EXCHANGE_ID", "binance")

    # Never write the operator's real .env from a test.
    written: dict[str, str] = {}
    monkeypatch.setattr(
        admin.settings, "_persist_env",
        lambda key, value: written.__setitem__(key, value),
    )
    yield written
    reset_position_monitor()
    venue_mod.reset_venue()


async def _track_real(symbol="BTC/USDT"):
    await get_position_monitor().track_manual_position(
        symbol=symbol, side="buy", qty=0.1, entry_price=70_000.0,
        stop_loss=68_000.0, take_profit=75_000.0, tab="real",
    )


def _request(venue, confirm=None):
    return admin.SwitchVenueRequest(venue=venue, confirm=confirm if confirm is not None else venue)


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_switching_persists_the_choice(_clean, monkeypatch):
    result = await admin.switch_venue(_request("bybit"))

    assert result["status"] == "success"
    assert result["previous"] == "binance"
    assert result["current"] == "bybit"
    # Persisted, so a restart keeps trading the venue the operator chose.
    assert _clean["EXCHANGE_ID"] == "bybit"


@pytest.mark.asyncio
async def test_the_client_is_REBUILT_not_reused(monkeypatch):
    """The old instance holds the other venue's markets, credentials and cached
    position mode. Reusing it would place orders with one venue's parameters
    against the other's API."""
    before = venue_mod.get_venue()
    assert before.id == "binance"

    await admin.switch_venue(_request("bybit"))

    after = venue_mod.get_venue()
    assert after is not before
    assert after.id == "bybit"


@pytest.mark.asyncio
async def test_switching_to_the_current_venue_is_a_no_op(_clean):
    result = await admin.switch_venue(_request("binance"))
    assert result["status"] == "unchanged"
    assert "EXCHANGE_ID" not in _clean


# ---------------------------------------------------------------------------
# The refusals
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_it_REFUSES_while_a_real_position_is_open(_clean):
    """THE test in this file.

    The position exists at the old venue and not at the new one. Nothing about
    this failure announces itself if the switch is allowed.
    """
    await _track_real()

    with pytest.raises(HTTPException) as exc:
        await admin.switch_venue(_request("bybit"))

    assert exc.value.status_code == 409
    assert "REAL position" in str(exc.value.detail)
    # And nothing was changed — a refusal that had already persisted would be
    # worse than no refusal.
    assert "EXCHANGE_ID" not in _clean
    assert venue_mod.configured_venue() == "binance"


@pytest.mark.asyncio
async def test_a_PAPER_position_does_not_block_the_switch(_clean):
    """A paper position has no venue counterpart at all, so there is nothing at
    either exchange for the switch to orphan."""
    await get_position_monitor().track_manual_position(
        symbol="SOL/USDT", side="buy", qty=1.0, entry_price=100.0,
        stop_loss=95.0, take_profit=110.0, tab="paper",
    )

    result = await admin.switch_venue(_request("bybit"))
    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_an_unsupported_venue_is_rejected(_clean):
    with pytest.raises(HTTPException) as exc:
        await admin.switch_venue(_request("kraken"))
    assert exc.value.status_code == 400
    assert "EXCHANGE_ID" not in _clean


@pytest.mark.asyncio
async def test_the_confirmation_must_name_the_venue(_clean):
    """This changes which account trades. A stray POST must not be able to."""
    with pytest.raises(HTTPException) as exc:
        await admin.switch_venue(_request("bybit", confirm="yes"))
    assert exc.value.status_code == 400
    assert "EXCHANGE_ID" not in _clean


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_reports_credentials_per_venue(monkeypatch):
    """Switching to a venue with no keys is legal but breaks every private call.
    The operator should see that BEFORE pressing, not at the first order."""
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_SECRET", "s")
    monkeypatch.delenv("BYBIT_API_KEY", raising=False)
    monkeypatch.delenv("BYBIT_SECRET", raising=False)

    status = await admin.venue_status()
    by_id = {v["id"]: v for v in status["venues"]}

    assert by_id["binance"]["credentialsConfigured"] is True
    assert by_id["bybit"]["credentialsConfigured"] is False
    # USE_TESTNET is on for the suite, so the variable in force is the testnet
    # one. Reporting the mainnet name while the process signs sandbox requests
    # sends the operator to set a key that is never read.
    assert by_id["bybit"]["keyVariable"] == "BYBIT_TESTNET_API_KEY"


@pytest.mark.asyncio
async def test_status_names_the_blocker_before_it_is_hit():
    await _track_real()

    status = await admin.venue_status()

    assert status["canSwitch"] is False
    assert status["realOpenPositions"] == 1
    assert "Close them first" in (status["blockedReason"] or "")


@pytest.mark.asyncio
async def test_switching_to_an_unconfigured_venue_warns_rather_than_failing(monkeypatch, _clean):
    """Market data needs no key, so the switch itself is valid. Saying so is the
    difference between a confusing first order and an expected one."""
    monkeypatch.delenv("BYBIT_API_KEY", raising=False)
    monkeypatch.delenv("BYBIT_SECRET", raising=False)

    result = await admin.switch_venue(_request("bybit"))

    assert result["status"] == "success"
    assert result["credentialsConfigured"] is False
    assert "BYBIT_TESTNET_API_KEY" in (result["warning"] or "")
