"""IBKR HK connector.

Wraps ib_async 2.1.0 around localhost:4002 (IB Gateway 10.37). Smoke
test on DUQ demo paper account passed 2026-04-15 via the standalone
script that proved ib_async connectivity before Bundle 2.

Design boundaries:
    - Lazy import of ib_async: module-level import would make the whole
      `execution.connectors` package unimportable on hosts that haven't
      installed the library (e.g. CI runners that only unit-test the
      engine against MockIBKRConnector). The import happens inside
      connect() -- callers that never construct a live connector never
      need the dependency.
    - Credentials: none in code. IB Gateway reads login from its own
      config; the connector knows only (host, port, clientId).
    - Error taxonomy: ib_async raises a flat Exception hierarchy with
      error codes in messages. This module maps the codes we care
      about (502 auth, 504 disconnect, 201 order rejected) into the
      typed exceptions in connectors.types so the engine branches on
      class, not string-match.
    - Read-only API toggle: IB Gateway's Read-Only API mode blocks
      order submission. Phase 2 paper trading requires Read-Only OFF.
      The connector surfaces the clear-order rejection code so Keith
      sees a loud failure instead of silent no-op.

Protocol conformance: methods match IBKRConnectorProtocol verbatim.
Tests exercise the mock connector at the same interface so swapping
this in for Phase 3 paper trading is a construction-site change only.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .types import (
    AccountSummary,
    AuthRequiredError,
    BrokerExecution,
    BrokerFillObservation,
    BrokerOpenOrder,
    BrokerOrderAck,
    BrokerOrderStatusEvent,
    BrokerPosition,
    BrokerRejectionError,
    ConnectionStatus,
    ConnectorError,
    DisconnectedError,
    POSITION_SOURCE_DISCONNECTED,
    POSITION_SOURCE_LIVE_REQ_POSITIONS,
    POSITION_SOURCE_TIMEOUT_FALLBACK,
    PositionSnapshot,
)


LOG = logging.getLogger("k2bi.connector.ibkr")


def _broker_id_str(value: Any) -> str:
    """Convert an ib_async orderId / permId to its canonical string.

    Codex round-13 P2: an unassigned orderId/permId comes back as
    int 0, which str() turns into the truthy "0". Recovery keys
    pending orders by broker_order_id / broker_perm_id -- multiple
    unassigned IDs would all collide on "oid:0" / "perm:0", so two
    in-flight orders could swap identities after a restart. Empty
    string means "no broker ID yet"; recovery falls through to
    trade_id matching instead.
    """
    if value in (None, 0, "0"):
        return ""
    return str(value)


# ib_async error codes we branch on. Keeping the list small and
# referenced from one place (here) so we don't sprinkle magic numbers.
# See IB TWS API error codes documentation.
_AUTH_ERROR_CODES = {502, 1100, 2110}       # login / cold-connect / connectivity
_DISCONNECT_ERROR_CODES = {504, 1102, 2103} # socket-level / market-data farm
_ORDER_REJECT_CODES = {201, 202, 203, 399}  # broker order rejection
_MAX_FILL_OBSERVATION_CACHE = 128


@dataclass(frozen=True)
class _FillObservationCacheEntry:
    epoch: int
    trade: Any
    observation: BrokerFillObservation


# Q34 (2026-04-21) bounded broker-API calls. Session F's run 3 hung
# for 3+ minutes on timed-out `open_orders_request` /
# `completed_orders_request` / `executions_request` after a
# connectivity flap. Every READ-PATH broker call is wrapped in
# asyncio.wait_for so the engine degrades cleanly instead of wedging.
#
# Split policy (architect-confirmed 2026-04-21):
#   - Reads (positions, open orders, marks, executions, status
#     history) return an empty sentinel on timeout. Caller / Q39-B
#     recovery treat empty results as "broker visibility limited"
#     (journal-authoritative fallback).
#   - Connect + account-summary-probe raise DisconnectedError on
#     timeout. Caller's reconnect/backoff cycle fires. The account
#     summary is handled as a probe (not a read) because the default
#     AccountSummary(cash=0, net_liq=0) is ambiguous with a real
#     zero-balance account and could mislead risk gates (MiniMax
#     Q34 R1 finding #5).
#
# SCOPE LIMITATION (2026-04-21): write-path calls -- submit_order,
# cancel_order, _await_parent_terminal -- are NOT wrapped by this
# change. They remain de-facto bounded by their existing ib_async
# polling loops (parent orderId/permId assignment: 50 iterations x
# 0.1s = 5s; child rejection cleanup: 30 x 0.1s = 3s; cancel broker
# confirmation: 30 x 0.1s = 3s). Total submit_order wall-time is
# bounded at ~15s worst case without explicit asyncio.wait_for. A
# formal write-path wrapper is deferred to Phase 4+ when execution-
# layer changes re-open; the Q34 scope per architect 2026-04-21 is
# the read path that hung Session F.
IBKR_CALL_TIMEOUT_SECONDS = 10.0


def _resolve_timeout(conn: "IBKRConnector") -> float:
    """Return the connector's configured per-call timeout. Defaults to
    IBKR_CALL_TIMEOUT_SECONDS; tests pass a sub-second override via
    the `timeout_seconds` constructor parameter (Q34 MiniMax R2
    finding #5)."""
    return conn._call_timeout_seconds


async def _bounded_read(
    conn: "IBKRConnector",
    awaitable: Any,
    *,
    call_name: str,
    empty: Any,
) -> Any:
    """Bound a broker-API READ call. On timeout: log + return `empty`
    so the caller falls back to journal-authoritative per Q39-B. Does
    NOT catch non-timeout exceptions -- those flow through the caller's
    existing _classify_and_raise path unchanged."""
    timeout = _resolve_timeout(conn)
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError:
        LOG.warning(
            "broker_api_timeout call=%s timeout=%.1fs; returning empty "
            "(Q34 journal-authoritative fallback)",
            call_name,
            timeout,
        )
        return empty


async def _bounded_probe(
    conn: "IBKRConnector",
    awaitable: Any,
    *,
    call_name: str,
) -> Any:
    """Bound a broker-API PROBE or CONNECT call. On timeout: raise
    DisconnectedError so the engine's reconnect/backoff cycle fires
    rather than silently degrading. Non-timeout exceptions flow
    through unchanged."""
    timeout = _resolve_timeout(conn)
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise DisconnectedError(
            f"broker_api_timeout call={call_name} timeout={timeout:.1f}s"
        ) from exc


class IBKRConnector:
    """Live ib_async-backed connector.

    Construct with broker coordinates; call `await connect()` before
    any other method. All reads + writes raise typed ConnectorError
    subclasses on failure; the engine's state machine owns reconnect
    backoff (per architect Q4 decision -- 5s start, 2x, 300s cap).
    """

    def __init__(
        self,
        *,
        account_id: str | None,
        host: str = "127.0.0.1",
        port: int = 4002,
        client_id: int = 1,
        default_currency: str = "HKD",
        timeout_seconds: float | None = None,
    ) -> None:
        """Construct with account scoping declared explicitly.

        Codex R16/R18 fixes introduced account_id filtering on
        get_positions / get_open_orders / get_executions_since /
        get_order_status_history, but the original constructor
        defaulted account_id=None, so any caller that forgot the
        kwarg silently lost the filter. Keith's architect ruling
        (post-R18): make this the K2Bi equivalent of Bundle 1's
        cash_only canonical helper -- discipline enforced at the
        type level. The kwarg is keyword-only + has no default, so a
        missing account decision is a TypeError at construction
        time, not a silent runtime bug.

        Single-account paper deployments pass account_id=None
        explicitly; multi-account live must pass the actual account
        id. Either is a conscious choice; neither is implicit.
        """
        self._host = host
        self._port = port
        self._client_id = client_id
        self._account_id = account_id
        self._default_currency = default_currency
        # Q34 MiniMax R2 finding #5 (2026-04-21): explicit constructor
        # parameter replaces the earlier _test_timeout_override getattr
        # lookup so the override path is part of the class contract,
        # not an undocumented runtime attribute that silently reverts
        # to the 10s production default after a rename.
        self._call_timeout_seconds: float = (
            float(timeout_seconds)
            if timeout_seconds is not None and timeout_seconds > 0
            else IBKR_CALL_TIMEOUT_SECONDS
        )

        self._ib: Any = None  # ib_async.IB instance, typed loosely to avoid import
        self._connected = False
        self._auth_required = False
        self._last_error: str | None = None
        self._external_fill_observer: Any = None
        self._fill_observations_by_trade_id: OrderedDict[
            tuple[int, int], _FillObservationCacheEntry
        ] = OrderedDict()
        self._fill_observation_cache_epoch = 0
        # fillEvent callbacks are synchronous; this protects the tiny
        # in-memory cache without awaiting inside the broker callback.
        self._fill_observations_lock = threading.Lock()

    # ---------- connection lifecycle ----------

    async def connect(self) -> None:
        self._clear_fill_observation_cache()
        try:
            import ib_async  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ConnectorImportError(
                "ib_async is not installed in this environment. "
                "Install with `pip install ib_async==2.1.0` or run the "
                "engine against MockIBKRConnector for unit tests."
            ) from exc

        if self._ib is None:
            self._ib = ib_async.IB()

        try:
            await _bounded_probe(
                self,
                self._ib.connectAsync(
                    host=self._host,
                    port=self._port,
                    clientId=self._client_id,
                    readonly=False,  # paper trading must allow order submission
                ),
                call_name="connect",
            )
        except Exception as exc:  # ib_async raises a flat hierarchy
            self._classify_and_raise(exc, phase="connect")

        # ib_async can report "connected" before auth settles. Poll
        # once for account summary to prove the session is usable --
        # if this call triggers 502, we learn about it now rather than
        # on the first order submission.
        try:
            await _bounded_probe(
                self,
                self._ib.reqAccountSummaryAsync(),
                call_name="post-connect-probe",
            )
        except Exception as exc:
            self._classify_and_raise(exc, phase="post-connect probe")

        self._connected = True
        self._auth_required = False
        self._last_error = None
        self._clear_fill_observation_cache()

    async def disconnect(self) -> None:
        if self._ib is not None and self._connected:
            try:
                self._ib.disconnect()
            except Exception as exc:  # pragma: no cover - shutdown path
                LOG.warning("ib_async disconnect raised: %s", exc)
        self._connected = False
        self._clear_fill_observation_cache()

    def connection_status(self) -> ConnectionStatus:
        return ConnectionStatus(
            connected=self._connected,
            auth_required=self._auth_required,
            last_error=self._last_error,
        )

    def set_external_fill_observer(self, callback: Any) -> None:
        if callback is not None:
            if not callable(callback):
                raise TypeError("external fill observer must be callable")
            if inspect.iscoroutinefunction(callback):
                raise TypeError("external fill observer must be synchronous")
            try:
                inspect.signature(callback).bind(object())
            except TypeError as exc:
                raise TypeError(
                    "external fill observer must accept one observation argument"
                ) from exc
        self._external_fill_observer = callback

    def _clear_fill_observation_cache(self) -> None:
        with self._fill_observations_lock:
            self._fill_observations_by_trade_id.clear()
            self._fill_observation_cache_epoch += 1

    def _fill_observation_cache_key(self, trade: Any) -> tuple[int, int]:
        return (self._fill_observation_cache_epoch, id(trade))

    # ---------- reads ----------

    async def get_account_summary(self) -> AccountSummary:
        """Aggregate the broker account snapshot.

        Codex round-5 P2: IBKR returns one row per (account, tag,
        currency), so multi-currency accounts have multiple
        TotalCashValue rows. The previous implementation overwrote
        cash/NLV on every row, giving whatever currency happened to
        come last. The correct filter is "BASE" currency for the
        account's base-currency snapshot (IBKR names the summary row
        for the base currency with currency="BASE"). If BASE is
        absent, fall back to the configured default_currency.
        """
        self._require_connected()
        try:
            # Q34 MiniMax R1 finding #5 (2026-04-21): use the PROBE
            # wrapper here, not the read wrapper. A default
            # AccountSummary(cash=0, net_liq=0) returned on timeout is
            # semantically indistinguishable from a real zero-balance
            # account; callers gating on NAV thresholds would reach
            # wrong conclusions. Raising DisconnectedError funnels the
            # call through the engine's reconnect/backoff cycle so a
            # cash/NLV snapshot only surfaces when broker visibility
            # is confirmed.
            rows = await _bounded_probe(
                self,
                self._ib.accountSummaryAsync(),
                call_name="account_summary",
            )
        except Exception as exc:
            self._classify_and_raise(exc, phase="account_summary")

        def _pick(
            preferred_currencies: tuple[str, ...],
            tag: str,
        ) -> Decimal | None:
            for target in preferred_currencies:
                for row in rows:
                    if getattr(row, "tag", "") != tag:
                        continue
                    if (
                        self._account_id
                        and getattr(row, "account", "") != self._account_id
                    ):
                        continue
                    row_currency = getattr(row, "currency", "") or ""
                    if row_currency.upper() != target.upper():
                        continue
                    try:
                        return Decimal(str(getattr(row, "value", "0")))
                    except Exception:  # pragma: no cover - broker edge
                        return None
            return None

        preferred = ("BASE", self._default_currency)
        cash = _pick(preferred, "TotalCashValue") or Decimal("0")
        net_liq = _pick(preferred, "NetLiquidation") or Decimal("0")

        # Determine which currency the picked values actually came
        # from. Codex R17 P3 / R20 P2: the prior implementation set
        # `currency` to the first non-BASE NetLiquidation row, which
        # mislabels BASE-sourced cash/NLV as (say) USD on multi-
        # currency accounts. Walk the same preferred order we used
        # for the values so cash/net_liq/currency are consistent.
        account_id = self._account_id or ""
        currency = self._default_currency
        for target in preferred:
            for row in rows:
                if getattr(row, "tag", "") != "NetLiquidation":
                    continue
                if (
                    self._account_id
                    and getattr(row, "account", "") != self._account_id
                ):
                    continue
                row_currency = getattr(row, "currency", "") or ""
                if row_currency.upper() != target.upper():
                    continue
                if not account_id:
                    account_id = getattr(row, "account", "") or ""
                currency = row_currency
                break
            else:
                continue
            break
        return AccountSummary(
            account_id=account_id,
            cash=cash,
            net_liquidation=net_liq,
            currency=currency,
        )

    async def get_positions(self) -> PositionSnapshot:
        """Broker positions filtered to the configured account_id.

        Codex R16 P1: `reqPositionsAsync` returns every position
        visible to the login, including sub-accounts and other client-
        IDs on the same Gateway. Without the account filter, engine
        startup / recovery / risk checks would see another account's
        holdings and either refuse to start (phantom_position on
        another account's ticker) or mis-size new orders. Matches the
        filter `get_account_summary` already applies.
        """
        try:
            self._require_connected()
            rows = await _bounded_read(
                self,
                self._ib.reqPositionsAsync(),
                call_name="positions",
                empty=None,
            )
            if rows is None:
                return PositionSnapshot(
                    positions=[],
                    valid=False,
                    source=POSITION_SOURCE_TIMEOUT_FALLBACK,
                    fetched_at=None,
                )
        except AuthRequiredError:
            raise
        except DisconnectedError:
            return PositionSnapshot(
                positions=[],
                valid=False,
                source=POSITION_SOURCE_DISCONNECTED,
                fetched_at=None,
            )
        except Exception as exc:
            try:
                self._classify_and_raise(exc, phase="positions")
            except AuthRequiredError:
                raise
            except DisconnectedError:
                return PositionSnapshot(
                    positions=[],
                    valid=False,
                    source=POSITION_SOURCE_DISCONNECTED,
                    fetched_at=None,
                )
            raise

        out: list[BrokerPosition] = []
        for row in rows:
            if self._account_id:
                row_account = getattr(row, "account", "") or ""
                if row_account and row_account != self._account_id:
                    continue
            contract = getattr(row, "contract", None)
            qty = int(getattr(row, "position", 0))
            avg = getattr(row, "avgCost", 0)
            symbol = getattr(contract, "symbol", "") if contract else ""
            if not symbol or qty == 0:
                continue
            out.append(
                BrokerPosition(
                    ticker=symbol,
                    qty=qty,
                    avg_price=Decimal(str(avg)),
                )
            )
        return PositionSnapshot(
            positions=out,
            valid=True,
            source=POSITION_SOURCE_LIVE_REQ_POSITIONS,
            fetched_at=datetime.now(timezone.utc),
        )

    async def get_open_orders(self) -> list[BrokerOpenOrder]:
        """Open orders filtered to the configured account_id.

        Codex R16 P1: `reqAllOpenOrdersAsync` returns every visible
        open order including other accounts/clients on the same
        Gateway. Without the account filter, recovery can refuse
        startup because another account has a live order, and EOD can
        cancel another account's DAY order if it happens to share a
        `k2bi:` prefix. Engine enforcement hangs on isolating this
        account's own activity.

        Spec B §5: a clientId=99 MasterClientID session can use
        `reqAllOpenOrders()` for cross-client visibility, but IBKR still
        binds `cancelOrder()` to the original placing clientId. Surgical
        orphan cleanup must reconnect on that placing clientId; only
        `reqGlobalCancel()` can cancel across clients, and that is too
        broad for per-order cleanup.
        """
        self._require_connected()
        try:
            rows = await _bounded_read(
                self,
                self._ib.reqAllOpenOrdersAsync(),
                call_name="open_orders",
                empty=[],
            )
        except Exception as exc:
            self._classify_and_raise(exc, phase="open_orders")

        out: list[BrokerOpenOrder] = []
        for trade in rows:
            order = getattr(trade, "order", None)
            contract = getattr(trade, "contract", None)
            status_obj = getattr(trade, "orderStatus", None)
            if order is None or contract is None:
                continue
            if self._account_id:
                order_account = getattr(order, "account", "") or ""
                if order_account and order_account != self._account_id:
                    continue
            tif = str(getattr(order, "tif", "") or "DAY").upper()
            out.append(
                BrokerOpenOrder(
                    broker_order_id=_broker_id_str(getattr(order, "orderId", 0)),
                    broker_perm_id=_broker_id_str(getattr(order, "permId", 0)),
                    ticker=getattr(contract, "symbol", ""),
                    side=str(getattr(order, "action", "")).lower(),
                    qty=int(getattr(order, "totalQuantity", 0)),
                    filled_qty=int(getattr(status_obj, "filled", 0)) if status_obj else 0,
                    limit_price=Decimal(str(getattr(order, "lmtPrice", "0"))),
                    status=str(getattr(status_obj, "status", "")) if status_obj else "",
                    submitted_at=None,
                    tif=tif,
                    client_tag=str(getattr(order, "orderRef", "") or ""),
                    # Q31: surface auxPrice so recovery's protective-
                    # stop validation can compare against the
                    # checkpoint's trigger_price. STP children carry
                    # their trigger in auxPrice; LMT parents leave
                    # it at 0 and Q31's price-drift check skips them.
                    aux_price=Decimal(str(getattr(order, "auxPrice", "0") or "0")),
                    # Q42: surface orderType so recovery's adoption
                    # gate can distinguish STP from other auxPrice-
                    # bearing types (TRAIL, TRAIL LIMIT). Empty default
                    # is FAIL-CLOSED for the adoption check.
                    order_type=str(getattr(order, "orderType", "") or ""),
                )
            )
        return out

    async def get_marks(self, tickers: list[str]) -> dict[str, Decimal]:
        self._require_connected()
        if not tickers:
            return {}
        import ib_async  # type: ignore[import-not-found]

        # Q34 MiniMax R3 finding #1 (2026-04-21): sequential per-ticker
        # awaits with 10s timeouts amplify worst-case wall time linearly
        # (50 tickers x 10s = 500s). Cap the TOTAL wall budget for the
        # whole marks pass so a slow market-data farm cannot stall
        # validation for minutes. Individual ticker timeouts still
        # apply; this is an outer envelope around the whole loop.
        aggregate_budget = min(
            3.0 * _resolve_timeout(self), 60.0
        )
        deadline = time.monotonic() + aggregate_budget
        out: dict[str, Decimal] = {}
        for ticker in tickers:
            if time.monotonic() >= deadline:
                LOG.warning(
                    "get_marks aggregate budget %.1fs exhausted; "
                    "remaining tickers skipped (Q34 R3 finding #1)",
                    aggregate_budget,
                )
                break
            contract = ib_async.Stock(ticker, "SMART", "USD")
            # Q35 (2026-04-21): qualify before mark-fetch so the
            # contract has a populated conId. Without this, Session G
            # Run 2 logged `Contract Stock(...) can't be hashed
            # because no 'conId' value exists` and the mark came back
            # empty, forcing fallback to the approved spec's
            # rule-derived limit. Qualify failure is non-fatal per
            # architect Q35 scope: log + skip the ticker, same as any
            # other mark-fetch error.
            try:
                qualified = await _bounded_read(
                    self,
                    self._ib.qualifyContractsAsync(contract),
                    call_name=f"qualify[{ticker}]",
                    empty=[],
                )
            except Exception as exc:
                LOG.warning(
                    "qualify failed for %s: %s; mark skipped", ticker, exc
                )
                continue
            # Q35 MiniMax R1 finding #1 (2026-04-21): an empty qualify
            # response is possible without an exception -- timeout
            # path returns []; the broker could also reply with no
            # qualified contracts for a delisted / unrecognized
            # symbol. Proceeding to reqTickersAsync with conId=0 would
            # reproduce the Session G "can't be hashed" warning. Skip
            # the ticker if qualify did not populate conId.
            if not qualified or not getattr(contract, "conId", 0):
                LOG.warning(
                    "qualify returned empty/unqualified for %s; "
                    "mark skipped",
                    ticker,
                )
                continue
            try:
                rows = await _bounded_read(
                    self,
                    self._ib.reqTickersAsync(contract),
                    call_name=f"tickers[{ticker}]",
                    empty=[],
                )
            except Exception as exc:
                # Market data errors are logged but non-fatal: validators
                # fall back to avg_price when a mark is missing.
                LOG.warning("mark fetch failed for %s: %s", ticker, exc)
                continue
            if not rows:
                # Q34 timeout path: reqTickersAsync timed out; skip.
                continue
            ticker_row = rows[0]
            raw_mark = getattr(ticker_row, "marketPrice", None)
            if raw_mark is None:
                continue
            # Q43 (2026-04-24): ib_async 2.1.0 exposes Ticker.marketPrice
            # as a method, not an attribute. Other price fields (last,
            # bid, ask, close) are float attributes; marketPrice and
            # midpoint are computed-method accessors. Pre-Q35 the
            # mark-fetch failed earlier (no conId), masking this latent
            # bug; post-Q35 the Ticker lands successfully and
            # Decimal(str(<bound method>)) raised ConversionSyntax on
            # first tick. Call if callable; pass through the value for
            # back-compat with hypothetical future library shapes.
            if callable(raw_mark):
                try:
                    mark = raw_mark()
                except Exception as exc:
                    LOG.warning(
                        "marketPrice() call failed for %s: %s", ticker, exc
                    )
                    continue
            else:
                mark = raw_mark
            if mark is None or mark != mark:  # NaN check
                continue
            try:
                out[ticker] = Decimal(str(mark))
            except (InvalidOperation, TypeError, ValueError) as exc:
                # Defense-in-depth: if a future ib_async returns a
                # non-numeric mark that slips past None/NaN guards, log
                # + skip rather than crash the tick loop.
                LOG.warning(
                    "Decimal conversion failed for %s mark=%r: %s",
                    ticker,
                    mark,
                    exc,
                )
                continue
        return out

    async def get_executions_since(
        self, since: datetime
    ) -> list[BrokerExecution]:
        self._require_connected()
        import ib_async  # type: ignore[import-not-found]

        since_utc = since.astimezone(timezone.utc) if since.tzinfo else since
        # Codex R16 P1 (applied transitively): ExecutionFilter accepts
        # an account param; populate when configured so cross-account
        # fills cannot leak into engine state.
        filter_kwargs: dict[str, Any] = {
            "time": since_utc.strftime("%Y%m%d %H:%M:%S"),
        }
        if self._account_id:
            filter_kwargs["acctCode"] = self._account_id
        exec_filter = ib_async.ExecutionFilter(**filter_kwargs)
        try:
            fills = await _bounded_read(
                self,
                self._ib.reqExecutionsAsync(exec_filter),
                call_name="executions_since",
                empty=[],
            )
        except Exception as exc:
            self._classify_and_raise(exc, phase="executions_since")

        out: list[BrokerExecution] = []
        for fill in fills:
            execution = getattr(fill, "execution", None)
            contract = getattr(fill, "contract", None)
            if execution is None or contract is None:
                continue
            # Defense-in-depth: if the filter is honored by ib_async
            # the list is already scoped, but double-check in case a
            # future lib version changes semantics.
            if self._account_id:
                exec_account = getattr(execution, "acctNumber", "") or ""
                if exec_account and exec_account != self._account_id:
                    continue
            out.append(
                BrokerExecution(
                    exec_id=str(getattr(execution, "execId", "")),
                    broker_order_id=str(getattr(execution, "orderId", "")),
                    broker_perm_id=str(getattr(execution, "permId", "")),
                    ticker=getattr(contract, "symbol", ""),
                    side=str(getattr(execution, "side", "")).lower(),
                    qty=int(getattr(execution, "shares", 0)),
                    price=Decimal(str(getattr(execution, "price", "0"))),
                    filled_at=_parse_ib_time(getattr(execution, "time", None)),
                )
            )
        return out

    async def get_order_status_history(
        self, since: datetime
    ) -> list[BrokerOrderStatusEvent]:
        """Union of currently-open-order status + recent completed
        orders.

        ib_async does not expose a direct "order status history" call;
        completed orders come from `reqCompletedOrdersAsync`. We merge
        with currently-open orders so recovery sees every order that
        was in flight at crash time in one pass.
        """
        self._require_connected()
        out: list[BrokerOrderStatusEvent] = []

        try:
            completed = await _bounded_read(
                self,
                self._ib.reqCompletedOrdersAsync(apiOnly=False),
                call_name="completed_orders",
                empty=[],
            )
        except Exception as exc:
            self._classify_and_raise(exc, phase="completed_orders")

        for trade in completed:
            order = getattr(trade, "order", None)
            status_obj = getattr(trade, "orderStatus", None)
            if order is None or status_obj is None:
                continue
            # Codex R16 P1: scope completed orders to the configured
            # account so recovery cannot reconcile against another
            # account's fills/cancels.
            if self._account_id:
                order_account = getattr(order, "account", "") or ""
                if order_account and order_account != self._account_id:
                    continue
            last_update = _last_log_time(trade) or datetime.now(timezone.utc)
            if last_update < (since.astimezone(timezone.utc) if since.tzinfo else since):
                continue
            out.append(
                BrokerOrderStatusEvent(
                    broker_order_id=_broker_id_str(getattr(order, "orderId", 0)),
                    broker_perm_id=_broker_id_str(getattr(order, "permId", 0)),
                    status=str(getattr(status_obj, "status", "")),
                    filled_qty=int(getattr(status_obj, "filled", 0)),
                    remaining_qty=int(getattr(status_obj, "remaining", 0)),
                    avg_fill_price=(
                        Decimal(str(getattr(status_obj, "avgFillPrice", "0")))
                        if getattr(status_obj, "avgFillPrice", None) is not None
                        else None
                    ),
                    last_update_at=last_update,
                    reason=getattr(status_obj, "whyHeld", None) or None,
                    client_tag=str(getattr(order, "orderRef", "") or ""),
                )
            )
        return out

    # ---------- writes ----------

    async def submit_order(
        self,
        ticker: str,
        side: str,
        qty: int,
        limit_price: Decimal | None,
        stop_loss: Decimal | None,
        time_in_force: str = "DAY",
        client_tag: str | None = None,
        order_type: str = "LMT",
    ) -> BrokerOrderAck:
        """Submit a limit or market order; if stop_loss is set, submit a
        linked stop child so the broker itself holds the protective stop.

        Codex round-3 P1: if the engine journals a stop and the broker
        does not hold one, a disconnect or process crash leaves the
        position completely unprotected. The bracket pattern below
        (parent.transmit=False, then child.transmit=True with
        parentId) tells IB Gateway to commit both orders atomically as
        a linked pair. The child is a GTC stop so it persists past the
        parent's DAY tif and survives engine restarts.

        Round-6 (2026-05-08): order_type branches the parent order
        construction. ``LMT`` requires a Decimal ``limit_price`` and
        constructs ``ib_async.LimitOrder``. ``MKT`` ignores
        ``limit_price`` (which may be a reference-price hint that
        downstream consumers don't honour) and constructs
        ``ib_async.MarketOrder``. Unknown order_type values are
        rejected explicitly to prevent silent fallthrough to the LMT
        path that would send the wrong broker contract.
        """
        self._require_connected()
        import ib_async  # type: ignore[import-not-found]

        contract = ib_async.Stock(ticker, "SMART", "USD")
        action = "BUY" if side.lower() == "buy" else "SELL"
        order_type_norm = (order_type or "LMT").strip().upper()
        if order_type_norm == "LMT":
            if limit_price is None:
                raise BrokerRejectionError(
                    "submit_order: LMT requires a Decimal limit_price; got None",
                    broker_reason="lmt_missing_limit_price",
                )
            parent = ib_async.LimitOrder(
                action,
                int(qty),
                float(limit_price),
                tif=time_in_force,
            )
        elif order_type_norm == "MKT":
            parent = ib_async.MarketOrder(
                action,
                int(qty),
                tif=time_in_force,
            )
        else:
            raise BrokerRejectionError(
                f"submit_order: unknown order_type {order_type!r}; "
                f"expected one of {{'LMT', 'MKT'}}",
                broker_reason="unknown_order_type",
            )
        # transmit=False means IB Gateway holds the parent until a
        # child order with transmit=True arrives referencing its
        # parentId. If there is no stop, parent transmits on its own.
        parent.transmit = stop_loss is None
        if client_tag is not None:
            parent.orderRef = client_tag

        try:
            parent_trade = self._ib.placeOrder(contract, parent)
            # ib_async only exposes the per-trade fillEvent after placeOrder()
            # returns the Trade. Attach before the first await so event-loop
            # delivered fills during orderId/permId waits are still observed.
            self._attach_external_fill_observer(parent_trade)
            # Wait for orderId assignment so the stop-child can
            # reference parentId. permId is not required yet; we wait
            # again after both orders transmit. Codex round-5 P1:
            # raise a typed error if IB never assigns the ID rather
            # than silently proceeding with orderId=0 (which would
            # break the parent/child linkage + make later recovery
            # matching impossible).
            for _ in range(50):
                if getattr(parent_trade.order, "orderId", 0):
                    break
                await asyncio.sleep(0.1)
            if not getattr(parent_trade.order, "orderId", 0):
                raise BrokerRejectionError(
                    "IB Gateway did not assign orderId within 5s of submit",
                    broker_reason="orderid_assignment_timeout",
                )

            child_trade = None
            if stop_loss is not None:
                stop_action = "SELL" if action == "BUY" else "BUY"
                child = ib_async.StopOrder(
                    stop_action,
                    int(qty),
                    float(stop_loss),
                    tif="GTC",  # outlive parent's DAY
                )
                child.parentId = parent_trade.order.orderId
                child.transmit = True  # transmit edge: commits pair
                if client_tag is not None:
                    child.orderRef = f"{client_tag}:stop"
                child_trade = self._ib.placeOrder(contract, child)
                # Same zero-await attach contract as the parent order.
                self._attach_external_fill_observer(child_trade)

            # Wait for permId on parent now that both orders are live.
            for _ in range(50):
                if getattr(parent_trade.order, "permId", 0):
                    break
                await asyncio.sleep(0.1)
            if not getattr(parent_trade.order, "permId", 0):
                raise BrokerRejectionError(
                    "IB Gateway did not assign permId within 5s of transmit",
                    broker_reason="permid_assignment_timeout",
                    broker_order_id=str(parent_trade.order.orderId),
                )

            # Codex round-8 P1 / round-9 P1: the stop child can be
            # rejected asynchronously by IBKR while a marketable parent
            # is already filling. Two paths below:
            #   - Parent still inactive/submitted  -> cancel parent + raise
            #   - Parent already live or filled    -> leave parent in place,
            #     return ack with a warning so the engine records the
            #     unprotected state and can escalate (kill / re-stop).
            warnings: list[str] = []
            if child_trade is not None:
                for _ in range(50):
                    child_status_obj = getattr(
                        child_trade, "orderStatus", None
                    )
                    child_status_str = (
                        str(getattr(child_status_obj, "status", ""))
                        if child_status_obj
                        else ""
                    )
                    if child_status_str == "Rejected":
                        parent_status_str = str(
                            getattr(parent_trade.orderStatus, "status", "")
                        )
                        parent_is_live = parent_status_str in {
                            "Filled",
                            "PartiallyFilled",
                            "Submitted",
                            "PreSubmitted",
                        }
                        reason = str(
                            getattr(child_status_obj, "whyHeld", "")
                            or "stop_child_rejected"
                        )
                        if parent_is_live:
                            warnings.append(
                                "protective_stop_child_rejected_parent_live:"
                                f"{reason}:parent_status={parent_status_str}"
                            )
                            break
                        try:
                            self._ib.cancelOrder(parent_trade.order)
                        except Exception as cancel_exc:  # pragma: no cover
                            LOG.warning(
                                "parent cancel after child rejection raised: %s",
                                cancel_exc,
                            )
                        # Codex R17 P1: cancelOrder is async at IBKR.
                        # Wait for broker-confirmed terminal status
                        # before raising BrokerRejectionError -- the
                        # engine's handler drops pending state on
                        # that exception, so a still-live parent in
                        # the unconfirmed window would become a
                        # phantom open order on restart.
                        await self._await_parent_terminal(
                            parent_trade, reason="child_rejected"
                        )
                        raise BrokerRejectionError(
                            "broker rejected protective stop child; parent cancelled",
                            broker_reason=reason,
                            broker_order_id=str(parent_trade.order.orderId),
                        )
                    if getattr(child_trade.order, "permId", 0):
                        break
                    await asyncio.sleep(0.1)
                if (
                    not getattr(child_trade.order, "permId", 0)
                    and not warnings
                ):
                    parent_status_str = str(
                        getattr(parent_trade.orderStatus, "status", "")
                    )
                    parent_is_live = parent_status_str in {
                        "Filled",
                        "PartiallyFilled",
                        "Submitted",
                        "PreSubmitted",
                    }
                    if parent_is_live:
                        warnings.append(
                            "protective_stop_child_permid_timeout_parent_live:"
                            f"parent_status={parent_status_str}"
                        )
                    else:
                        try:
                            self._ib.cancelOrder(parent_trade.order)
                        except Exception as cancel_exc:  # pragma: no cover
                            LOG.warning(
                                "parent cancel after child timeout raised: %s",
                                cancel_exc,
                            )
                        # Codex R17 P1: same async-cancel concern as
                        # the sibling rejected branch above.
                        await self._await_parent_terminal(
                            parent_trade, reason="child_permid_timeout"
                        )
                        raise BrokerRejectionError(
                            "stop child did not receive permId within 5s; parent cancelled",
                            broker_reason="stop_child_permid_timeout",
                            broker_order_id=str(parent_trade.order.orderId),
                        )
        except Exception as exc:
            self._classify_and_raise(exc, phase="submit")

        status = getattr(parent_trade.orderStatus, "status", "")
        stop_broker_order_id = None
        stop_broker_perm_id = None
        stop_price = None
        if (
            child_trade is not None
            and not warnings
            and getattr(child_trade.order, "permId", 0)
        ):
            stop_broker_order_id = str(child_trade.order.orderId)
            stop_broker_perm_id = str(child_trade.order.permId)
            stop_price = stop_loss
        return BrokerOrderAck(
            broker_order_id=str(parent_trade.order.orderId),
            broker_perm_id=str(parent_trade.order.permId),
            submitted_at=datetime.now(timezone.utc),
            status=status,
            stop_broker_order_id=stop_broker_order_id,
            stop_broker_perm_id=stop_broker_perm_id,
            stop_price=stop_price,
            warnings=tuple(warnings),
        )

    def _attach_external_fill_observer(self, trade: Any) -> None:
        fill_event = getattr(trade, "fillEvent", None)
        if fill_event is None:
            LOG.warning("trade fillEvent unavailable; external-fill observation skipped")
            self._emit_fill_event_unavailable_observation(trade)
            return
        try:
            fill_event += self._on_trade_fill_event
        except Exception as exc:  # pragma: no cover - broker edge
            LOG.warning("trade fillEvent subscription failed: %s", exc)

    def _emit_fill_event_unavailable_observation(self, trade: Any) -> None:
        if self._external_fill_observer is None:
            return
        order = getattr(trade, "order", None)
        contract = getattr(trade, "contract", None)
        now = datetime.now(timezone.utc)
        observation = BrokerFillObservation(
            ticker=str(getattr(contract, "symbol", "") or ""),
            side=_normalize_execution_side(str(getattr(order, "action", "") or "")),
            qty=0,
            price=Decimal("0"),
            filled_at=now,
            observed_at=now,
            broker_order_id=_broker_id_str(getattr(order, "orderId", 0)),
            broker_perm_id=_broker_id_str(getattr(order, "permId", 0)),
            exec_id="fill_event_unavailable",
            client_tag=str(getattr(order, "orderRef", "") or ""),
            source="fill_event_unavailable",
        )
        try:
            self._external_fill_observer(observation)
        except Exception as exc:  # pragma: no cover - observer owns details
            LOG.warning("external fill observer raised: %s", exc)

    def _on_trade_fill_event(self, trade: Any, fill: Any) -> None:
        if self._external_fill_observer is None:
            return
        with self._fill_observations_lock:
            cache_epoch = self._fill_observation_cache_epoch
        try:
            observation = self._build_fill_observation(trade, fill)
        except Exception as exc:
            LOG.warning("external fill observation conversion failed: %s", exc)
            observation = self._build_malformed_fill_observation(trade, fill)
            try:
                self._external_fill_observer(observation)
            except Exception as observer_exc:  # pragma: no cover - observer owns details
                LOG.warning("external fill observer raised: %s", observer_exc)
            return
        with self._fill_observations_lock:
            if cache_epoch != self._fill_observation_cache_epoch:
                LOG.warning(
                    "discarding fill observation: cache epoch changed "
                    "during callback"
                )
                return
            cache_key = (cache_epoch, id(trade))
            self._fill_observations_by_trade_id[cache_key] = _FillObservationCacheEntry(
                epoch=cache_epoch,
                trade=trade,
                observation=observation,
            )
            while len(self._fill_observations_by_trade_id) > _MAX_FILL_OBSERVATION_CACHE:
                self._fill_observations_by_trade_id.popitem(last=False)
        # Observer may journal to disk. Keep it outside the cache lock so
        # the broker callback thread only holds the lock for dict mutation.
        try:
            self._external_fill_observer(observation)
        except Exception as exc:  # pragma: no cover - observer owns details
            LOG.warning("external fill observer raised: %s", exc)

    def _filled_ack_if_trade_filled(
        self,
        trade: Any,
        *,
        submitted_at: datetime,
    ) -> BrokerOrderAck | None:
        status = str(getattr(getattr(trade, "orderStatus", None), "status", ""))
        if status != "Filled":
            return None
        with self._fill_observations_lock:
            cache_key = self._fill_observation_cache_key(trade)
            entry = self._fill_observations_by_trade_id.pop(cache_key, None)
            if entry is None:
                stale_keys = [
                    key
                    for key in self._fill_observations_by_trade_id
                    if key[1] == id(trade)
                ]
                if stale_keys:
                    entry = self._fill_observations_by_trade_id.pop(stale_keys[-1])
        observation = self._validated_cached_fill_observation(trade, entry)
        if observation is None:
            if entry is None:
                LOG.warning(
                    "filled trade has no cached fill observation; no safe ack"
                )
            return None
        if not observation.broker_order_id or not observation.broker_perm_id:
            LOG.warning(
                "cached fill observation lacks broker ids; no safe ack "
                "order_id=%r perm_id=%r",
                observation.broker_order_id,
                observation.broker_perm_id,
            )
            return None
        return BrokerOrderAck(
            broker_order_id=observation.broker_order_id,
            broker_perm_id=observation.broker_perm_id,
            submitted_at=submitted_at,
            status=status,
        )

    def _validated_cached_fill_observation(
        self,
        trade: Any,
        entry: _FillObservationCacheEntry | None,
    ) -> BrokerFillObservation | None:
        if entry is None:
            return None
        if entry.epoch != self._fill_observation_cache_epoch:
            LOG.warning(
                "discarding cached fill observation: cache epoch mismatch "
                "entry=%s current=%s",
                entry.epoch,
                self._fill_observation_cache_epoch,
            )
            return None
        if entry.trade is not trade:
            LOG.warning("discarding cached fill observation: trade object mismatch")
            return None
        observation = entry.observation
        reason = self._fill_observation_mismatch_reason(trade, observation)
        if reason is not None:
            LOG.warning("discarding cached fill observation: %s", reason)
            return None
        return observation

    def _fill_observation_mismatch_reason(
        self,
        trade: Any,
        observation: BrokerFillObservation,
    ) -> str | None:
        current_tickers = _trade_tickers(trade)
        observation_ticker = observation.ticker.upper()
        if current_tickers and observation_ticker not in current_tickers:
            return (
                f"ticker mismatch observation={observation.ticker!r} "
                f"current={sorted(current_tickers)!r}"
            )

        order_ids = _trade_broker_ids(trade, "orderId")
        if order_ids and observation.broker_order_id not in order_ids:
            return (
                f"broker_order_id mismatch observation={observation.broker_order_id!r} "
                f"current={sorted(order_ids)!r}"
            )

        perm_ids = _trade_broker_ids(trade, "permId")
        if perm_ids and observation.broker_perm_id not in perm_ids:
            return (
                f"broker_perm_id mismatch observation={observation.broker_perm_id!r} "
                f"current={sorted(perm_ids)!r}"
            )

        exec_ids = _trade_exec_ids(trade)
        if exec_ids and observation.exec_id not in exec_ids:
            return (
                f"exec_id mismatch observation={observation.exec_id!r} "
                f"current={sorted(exec_ids)!r}"
            )
        return None

    def _build_fill_observation(self, trade: Any, fill: Any) -> BrokerFillObservation:
        execution = getattr(fill, "execution", None)
        if execution is None:
            raise ValueError("fill has no execution")
        contract = getattr(trade, "contract", None) or getattr(fill, "contract", None)
        order = getattr(trade, "order", None)
        ticker = str(getattr(contract, "symbol", "") or "")
        side = _normalize_execution_side(str(getattr(execution, "side", "") or ""))
        qty = int(Decimal(str(getattr(execution, "shares", 0))))
        price = Decimal(str(getattr(execution, "price", "0")))
        order_id = _broker_id_str(
            getattr(execution, "orderId", None)
            or getattr(order, "orderId", 0)
        )
        perm_id = _broker_id_str(
            getattr(execution, "permId", None)
            or getattr(order, "permId", 0)
        )
        return BrokerFillObservation(
            ticker=ticker,
            side=side,
            qty=qty,
            price=price,
            filled_at=_parse_ib_time(getattr(execution, "time", None)),
            observed_at=datetime.now(timezone.utc),
            broker_order_id=order_id,
            broker_perm_id=perm_id,
            exec_id=str(getattr(execution, "execId", "") or ""),
            client_tag=str(getattr(order, "orderRef", "") or ""),
            source="trade_fill_event",
        )

    def _build_malformed_fill_observation(
        self,
        trade: Any,
        fill: Any,
    ) -> BrokerFillObservation:
        execution = getattr(fill, "execution", None)
        contract = getattr(trade, "contract", None) or getattr(fill, "contract", None)
        order = getattr(trade, "order", None)
        now = datetime.now(timezone.utc)
        side = _normalize_execution_side(
            str(
                getattr(execution, "side", None)
                or getattr(order, "action", "")
                or ""
            )
        )
        filled_at = now
        if execution is not None:
            try:
                filled_at = _parse_ib_time(getattr(execution, "time", None))
            except Exception:
                filled_at = now
        return BrokerFillObservation(
            ticker=str(getattr(contract, "symbol", "") or ""),
            side=side or "unknown",
            qty=0,
            price=Decimal("0"),
            filled_at=filled_at,
            observed_at=now,
            broker_order_id=_broker_id_str(
                getattr(execution, "orderId", None)
                or getattr(order, "orderId", 0)
            ),
            broker_perm_id=_broker_id_str(
                getattr(execution, "permId", None)
                or getattr(order, "permId", 0)
            ),
            exec_id=str(
                getattr(execution, "execId", "") or "fill_event_conversion_failed"
            ),
            client_tag=str(getattr(order, "orderRef", "") or ""),
            source="trade_fill_event",
        )

    async def submit_standalone_stop_order(
        self,
        *,
        ticker: str,
        side: str,
        qty: int,
        stop_price: Decimal,
        time_in_force: str = "GTC",
        client_tag: str | None = None,
    ) -> BrokerOrderAck:
        """Submit a recovery-only standalone STP for an existing position.

        Normal entry submits must use submit_order() so the STP is a
        parent-bound child. This method exists only for recovery repair
        when the position already exists and needs a fresh protective
        stop attached by operator/recovery context.
        """

        self._require_connected()
        import ib_async  # type: ignore[import-not-found]

        contract = ib_async.Stock(ticker, "SMART", "USD")
        action = "SELL" if side.lower() == "sell" else "BUY"
        stop = ib_async.StopOrder(
            action,
            int(qty),
            float(stop_price),
            tif=time_in_force,
        )
        stop.parentId = 0
        stop.transmit = True
        if client_tag is not None:
            stop.orderRef = client_tag

        try:
            # Local placeOrder-call time. IBKR may fill before orderId/permId
            # assignment, so the broker has no separate submit timestamp here.
            submitted_at = datetime.now(timezone.utc)
            trade = self._ib.placeOrder(contract, stop)
            # ib_async returns the Trade synchronously; attach before any await.
            self._attach_external_fill_observer(trade)
            for _ in range(50):
                if getattr(trade.order, "orderId", 0):
                    break
                await asyncio.sleep(0.1)
            if not getattr(trade.order, "orderId", 0):
                filled_ack = self._filled_ack_if_trade_filled(
                    trade,
                    submitted_at=submitted_at,
                )
                if filled_ack is not None:
                    return filled_ack
                try:
                    self._ib.cancelOrder(trade.order)
                except Exception as cancel_exc:  # pragma: no cover
                    LOG.warning(
                        "standalone stop cancel after orderId timeout raised: %s",
                        cancel_exc,
                    )
                await self._await_parent_terminal(
                    trade, reason="standalone_stop_orderid_timeout"
                )
                filled_ack = self._filled_ack_if_trade_filled(
                    trade,
                    submitted_at=submitted_at,
                )
                if filled_ack is not None:
                    return filled_ack
                raise BrokerRejectionError(
                    "IB Gateway did not assign orderId within 5s of standalone stop",
                    broker_reason="standalone_stop_orderid_timeout",
                )
            for _ in range(50):
                if getattr(trade.order, "permId", 0):
                    break
                await asyncio.sleep(0.1)
            if not getattr(trade.order, "permId", 0):
                filled_ack = self._filled_ack_if_trade_filled(
                    trade,
                    submitted_at=submitted_at,
                )
                if filled_ack is not None:
                    return filled_ack
                try:
                    self._ib.cancelOrder(trade.order)
                except Exception as cancel_exc:  # pragma: no cover
                    LOG.warning(
                        "standalone stop cancel after permId timeout raised: %s",
                        cancel_exc,
                    )
                await self._await_parent_terminal(
                    trade, reason="standalone_stop_permid_timeout"
                )
                filled_ack = self._filled_ack_if_trade_filled(
                    trade,
                    submitted_at=submitted_at,
                )
                if filled_ack is not None:
                    return filled_ack
                raise BrokerRejectionError(
                    "IB Gateway did not assign permId within 5s of standalone stop",
                    broker_reason="standalone_stop_permid_timeout",
                    broker_order_id=str(trade.order.orderId),
                )
        except Exception as exc:
            self._classify_and_raise(exc, phase="submit_standalone_stop")

        status = getattr(trade.orderStatus, "status", "")
        return BrokerOrderAck(
            broker_order_id=str(trade.order.orderId),
            broker_perm_id=str(trade.order.permId),
            submitted_at=submitted_at,
            status=status,
        )

    async def cancel_order(self, broker_order_id: str) -> None:
        self._require_connected()
        try:
            trades = self._ib.openTrades()
        except Exception as exc:
            self._classify_and_raise(exc, phase="cancel_lookup")
        for trade in trades:
            if str(trade.order.orderId) == broker_order_id:
                try:
                    self._ib.cancelOrder(trade.order)
                except Exception as exc:
                    self._classify_and_raise(exc, phase="cancel")
                return
        # Unknown order id: not an error -- EOD cancel can race with a
        # fill, and the engine's recovery path will observe the
        # terminal status on next tick.
        LOG.info(
            "cancel_order: no open order with broker_order_id=%s; "
            "likely already terminal",
            broker_order_id,
        )

    async def _await_parent_terminal(
        self, parent_trade: Any, *, reason: str
    ) -> None:
        """Poll up to ~3s for parent order to reach a terminal status.

        Codex R17 P1: cancelOrder is async. After requesting cancel
        we must give the broker time to confirm (Cancelled / Rejected
        / Inactive) before telling the engine the order is dead. If
        the terminal never arrives within the window we still proceed
        -- the engine's tick-based reconcile catches up on next poll
        -- but we log so the audit trail records the uncertainty.
        """
        terminal = {"Cancelled", "ApiCancelled", "Rejected", "Inactive"}
        for _ in range(30):
            status = str(
                getattr(parent_trade.orderStatus, "status", "")
            )
            if status in terminal:
                return
            await asyncio.sleep(0.1)
        LOG.warning(
            "parent cancel (%s) not broker-confirmed within 3s; "
            "engine poll will reconcile on next tick",
            reason,
        )

    # ---------- error classification ----------

    def _classify_and_raise(self, exc: Exception, *, phase: str) -> None:
        # If the exception is already one of our typed ConnectorError
        # subclasses, do not re-classify (that would demote
        # BrokerRejectionError to a generic DisconnectedError when
        # the "code" we extract from a message-less internal error is
        # None).
        if isinstance(exc, ConnectorError):
            # Still record the last error string for operator visibility,
            # but re-raise the original typed exception.
            self._last_error = f"{phase}: {exc}"
            raise exc
        code = _extract_error_code(exc)
        message = f"ib_async error during {phase}: code={code} err={exc}"
        self._last_error = message
        if code in _AUTH_ERROR_CODES:
            self._connected = False
            self._auth_required = True
            self._clear_fill_observation_cache()
            raise AuthRequiredError(message) from exc
        if code in _DISCONNECT_ERROR_CODES:
            self._connected = False
            self._clear_fill_observation_cache()
            raise DisconnectedError(message) from exc
        if code in _ORDER_REJECT_CODES:
            raise BrokerRejectionError(
                message,
                broker_reason=str(exc),
            ) from exc
        # Unknown code: treat as disconnect so the engine pauses + reconnects
        # instead of auto-retrying a busted session.
        self._connected = False
        self._clear_fill_observation_cache()
        raise DisconnectedError(message) from exc

    def _require_connected(self) -> None:
        if self._auth_required:
            raise AuthRequiredError("IB Gateway requires re-login")
        if not self._connected:
            raise DisconnectedError("not connected")


class ConnectorImportError(ConnectorError):
    """ib_async missing at connect-time. Tests use MockIBKRConnector.

    Codex R21 P2: inherits from ConnectorError (not RuntimeError) so
    the engine's _run_init exception handler catches it alongside
    AuthRequiredError / DisconnectedError and halts with a clean
    journal entry, instead of the exception escaping the state
    machine.
    """


def _extract_error_code(exc: Exception) -> int | None:
    """Pull the numeric IB error code off whatever ib_async raised.

    ib_async commonly exposes `.errorCode` on its errors; fall back to
    scanning the string for "code=NNN" or leading "NNN:". None if nothing
    parseable is found -- caller treats as unknown.
    """
    code = getattr(exc, "errorCode", None)
    if isinstance(code, int):
        return code
    msg = str(exc)
    import re

    m = re.search(r"(?:code=|reqId=\d+\s+)(\d{3,5})", msg)
    if m:
        return int(m.group(1))
    m = re.match(r"^(\d{3,5})\b", msg.strip())
    if m:
        return int(m.group(1))
    return None


def _iter_trade_execution_objects(trade: Any):
    for attr_name in ("fills", "executions"):
        rows = getattr(trade, attr_name, None)
        if rows is None or isinstance(rows, (str, bytes)):
            continue
        try:
            iterator = iter(rows)
        except TypeError:
            continue
        for row in iterator:
            execution = getattr(row, "execution", row)
            if execution is not None:
                yield execution


def _trade_tickers(trade: Any) -> set[str]:
    out: set[str] = set()
    contract = getattr(trade, "contract", None)
    symbol = str(getattr(contract, "symbol", "") or "").upper()
    if symbol:
        out.add(symbol)
    for attr_name in ("fills", "executions"):
        rows = getattr(trade, attr_name, None)
        if rows is None or isinstance(rows, (str, bytes)):
            continue
        try:
            iterator = iter(rows)
        except TypeError:
            continue
        for row in iterator:
            row_contract = getattr(row, "contract", None)
            row_symbol = str(getattr(row_contract, "symbol", "") or "").upper()
            if row_symbol:
                out.add(row_symbol)
    return out


def _trade_broker_ids(trade: Any, field: str) -> set[str]:
    out: set[str] = set()
    order = getattr(trade, "order", None)
    order_id = _broker_id_str(getattr(order, field, 0))
    if order_id:
        out.add(order_id)
    for execution in _iter_trade_execution_objects(trade):
        execution_id = _broker_id_str(getattr(execution, field, 0))
        if execution_id:
            out.add(execution_id)
    return out


def _trade_exec_ids(trade: Any) -> set[str]:
    out: set[str] = set()
    for execution in _iter_trade_execution_objects(trade):
        exec_id = str(getattr(execution, "execId", "") or "")
        if exec_id:
            out.add(exec_id)
    return out


def _parse_ib_time(value: Any) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value)
    for fmt in ("%Y%m%d  %H:%M:%S", "%Y%m%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.now(timezone.utc)


def _normalize_execution_side(value: str) -> str:
    side = value.strip().upper()
    if side in {"BOT", "BUY", "BOUGHT"}:
        return "buy"
    if side in {"SLD", "SELL", "SOLD"}:
        return "sell"
    return side.lower()


def _last_log_time(trade: Any) -> datetime | None:
    log = getattr(trade, "log", None)
    if not log:
        return None
    last = log[-1]
    t = getattr(last, "time", None)
    if t is None:
        return None
    if isinstance(t, datetime):
        return t.astimezone(timezone.utc) if t.tzinfo else t.replace(tzinfo=timezone.utc)
    return _parse_ib_time(t)


__all__ = ["IBKRConnector", "ConnectorImportError"]
