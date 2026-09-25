import logging
import unittest
from decimal import Decimal
from types import SimpleNamespace

from execution.connectors.ibkr import IBKRConnector


class _FakeFillEvent:
    """Append-only stand-in for ib_async's fillEvent Event."""

    def __init__(self):
        self._handlers = []

    def __iadd__(self, handler):
        self._handlers.append(handler)
        return self

    def emit(self, *args, **kwargs):
        for handler in list(self._handlers):
            handler(*args, **kwargs)

    @property
    def handler_count(self):
        return len(self._handlers)


def _make_trade(*, account="DU_TEST", order_ref="k2bi:strat-a:trade-1",
                fill_event=None, oid=1):
    order = SimpleNamespace(
        account=account,
        orderRef=order_ref,
        orderId=oid,
        permId=oid,
        totalQuantity=10,
        tif="DAY",
        action="BUY",
        lmtPrice="1.0",
        auxPrice="0",
        orderType="LMT",
    )
    contract = SimpleNamespace(symbol="AAPL")
    status = SimpleNamespace(filled=0, status="Submitted")
    trade = SimpleNamespace(
        order=order,
        contract=contract,
        orderStatus=status,
    )
    if fill_event is not None:
        trade.fillEvent = fill_event
    return trade


class _FakeIb:
    def __init__(self, trades):
        self._trades = trades

    async def reqAllOpenOrdersAsync(self):
        return list(self._trades)


class _TrackingConnector(IBKRConnector):
    """Subclass that records observer deliveries without broker calls."""

    def __init__(self, trades, **kwargs):
        super().__init__(**kwargs)
        self._ib = _FakeIb(trades)
        self._connected = True
        self.delivered = []
        self.unavailable = []

    def _emit_fill_event_unavailable_observation(self, trade):
        self.unavailable.append(trade)

    def _on_trade_fill_event(self, trade, fill):
        # Bound callback invoked when the fake event emits.
        self.delivered.append((trade, fill))


class RecoveredFillObserverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    async def test_repeat_reads_attach_only_one_callback(self):
        event = _FakeFillEvent()
        trade = _make_trade(fill_event=event)
        connector = _TrackingConnector([trade], account_id="DU_TEST")

        await connector.get_open_orders()
        await connector.get_open_orders()
        await connector.get_open_orders()

        self.assertEqual(event.handler_count, 1)

    async def test_recovered_stop_callback_is_delivered(self):
        event = _FakeFillEvent()
        trade = _make_trade(
            fill_event=event,
            order_ref="k2bi:strat-a:trade-9:stop",
        )
        connector = _TrackingConnector([trade], account_id="DU_TEST")

        await connector.get_open_orders()
        self.assertEqual(event.handler_count, 1)

        sentinel = SimpleNamespace(execution=SimpleNamespace(shares=3))
        event.emit(trade, sentinel)

        self.assertEqual(len(connector.delivered), 1)
        delivered_trade, delivered_fill = connector.delivered[0]
        self.assertIs(delivered_trade, trade)
        self.assertIs(delivered_fill, sentinel)

    async def test_foreign_account_not_attached(self):
        event = _FakeFillEvent()
        trade = _make_trade(account="DU_OTHER", fill_event=event)
        connector = _TrackingConnector([trade], account_id="DU_TEST")

        orders = await connector.get_open_orders()

        self.assertEqual(event.handler_count, 0)
        # Preserve existing filtering behavior: foreign account rows
        # are still excluded from output when they carry an account.
        self.assertEqual(orders, [])

    async def test_unknown_account_value_not_attached(self):
        event = _FakeFillEvent()
        trade = _make_trade(account="", fill_event=event)
        connector = _TrackingConnector([trade], account_id="DU_TEST")

        await connector.get_open_orders()

        self.assertEqual(event.handler_count, 0)

    async def test_non_k2bi_order_not_attached(self):
        event = _FakeFillEvent()
        trade = _make_trade(fill_event=event, order_ref="manual-order-7")
        connector = _TrackingConnector([trade], account_id="DU_TEST")

        await connector.get_open_orders()

        self.assertEqual(event.handler_count, 0)

    async def test_distinct_trades_each_attach(self):
        event_one = _FakeFillEvent()
        event_two = _FakeFillEvent()
        trade_one = _make_trade(fill_event=event_one, oid=1)
        trade_two = _make_trade(
            fill_event=event_two,
            order_ref="k2bi:strat-b:trade-2",
            oid=2,
        )
        connector = _TrackingConnector(
            [trade_one, trade_two], account_id="DU_TEST"
        )

        await connector.get_open_orders()

        self.assertEqual(event_one.handler_count, 1)
        self.assertEqual(event_two.handler_count, 1)

    async def test_failed_attach_can_be_retried(self):
        class _FailingFillEvent:
            def __init__(self):
                self._handlers = []
                self.fail_next = True

            def __iadd__(self, handler):
                if self.fail_next:
                    self.fail_next = False
                    raise RuntimeError("boom")
                self._handlers.append(handler)
                return self

            @property
            def handler_count(self):
                return len(self._handlers)

        event = _FailingFillEvent()
        trade = _make_trade(fill_event=event)
        connector = _TrackingConnector([trade], account_id="DU_TEST")

        await connector.get_open_orders()
        self.assertEqual(event.handler_count, 0)
        # Failed subscription must not leave a success marker behind.
        self.assertIsNone(
            getattr(trade, "_k2bi_fill_observer_callback", None)
        )

        await connector.get_open_orders()
        self.assertEqual(event.handler_count, 1)

    async def test_missing_fill_event_retains_unavailable_reporting(self):
        trade = _make_trade(fill_event=None)
        connector = _TrackingConnector([trade], account_id="DU_TEST")

        await connector.get_open_orders()

        self.assertEqual(len(connector.unavailable), 1)
        self.assertIs(connector.unavailable[0], trade)
        self.assertIsNone(
            getattr(trade, "_k2bi_fill_observer_callback", None)
        )

    async def test_empty_account_id_skips_attachment_but_returns_orders(self):
        event = _FakeFillEvent()
        trade = _make_trade(fill_event=event)
        connector = _TrackingConnector([trade], account_id="")

        orders = await connector.get_open_orders()

        self.assertEqual(event.handler_count, 0)
        self.assertEqual(len(orders), 1)

    async def test_trade_rejecting_attributes_subscribes_only_once(self):
        class SlotTrade:
            __slots__ = ('order', 'contract', 'orderStatus', 'fillEvent')
        source = _make_trade(fill_event=_FakeFillEvent())
        trade = SlotTrade()
        for name in SlotTrade.__slots__:
            setattr(trade,name,getattr(source,name))
        connector = _TrackingConnector([trade],account_id="DU_TEST")
        await connector.get_open_orders()
        await connector.get_open_orders()
        trade.fillEvent.emit(trade,object())
        self.assertEqual(trade.fillEvent.handler_count,1)
        self.assertEqual(len(connector.delivered),1)

    async def test_subscription_identity_does_not_retain_weakrefable_trade(self):
        import gc
        import weakref
        class Trade:
            pass
        source = _make_trade(fill_event=_FakeFillEvent())
        trade = Trade()
        trade.__dict__.update(source.__dict__)
        connector = _TrackingConnector([trade],account_id="DU_TEST")
        await connector.get_open_orders()
        reference = weakref.ref(trade)
        connector._ib._trades.clear()
        del trade
        gc.collect()
        self.assertIsNone(reference())
        self.assertEqual(connector._fill_observer_trade_refs,{})



if __name__ == "__main__":
    unittest.main()
