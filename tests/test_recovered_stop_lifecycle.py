"""Offline restart, execution proof and crash-boundary regression tests."""
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

from execution.connectors.mock import MockIBKRConnector
from execution.connectors.types import BrokerExecution, BrokerOpenOrder
from execution.engine.main import Engine, EngineConfig, EngineState, ProtectiveStopRecord
from execution.engine.stop_reconciliation import read_stop_history, unresolved_stop_barriers
from execution.engine.recovery import _positions_from_journal
from execution.journal.writer import JournalWriter
from execution.strategies import loader
from tests.test_engine_strategy_stopped_out import CONFIG, STRATEGY_ID, _write_strategy, _position


class RecoveredStopLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.strategies = self.base / 'strategies'; self.strategies.mkdir()
        self.path = _write_strategy(self.strategies)
        self.journal = JournalWriter(self.base / 'journal', git_sha='offline-test')
        self.connector = MockIBKRConnector()
        self.now = datetime.now(timezone.utc)
        self.record = ProtectiveStopRecord('G', 2000001, Decimal('30'), 2000000,
            self.now, STRATEGY_ID, 'T-restart', '1001', 71)
        self.execution = BrokerExecution('stop-exec', '1001','2000001','G','sld',71,
                                         Decimal('29.77'), self.now)
        self.expected = {'ticker':'G','strategy':STRATEGY_ID,'parent_trade_id':'T-restart',
            'client_tag':f'{STRATEGY_ID}:T-restart:stop','trigger_price':'30'}
        self.journal.append('engine_recovered',payload={
            'adopted_positions':[{'ticker':'G','qty':71,'avg_price':'34.50'},
                                 {'ticker':'SPY','qty':2,'avg_price':'707.72'}],
            'expected_stop_children':[self.expected]})

    def engine(self):
        engine = Engine(connector=self.connector, journal=self.journal,
            validator_config=CONFIG, engine_config=EngineConfig(
                strategies_dir=self.strategies, kill_path=self.base/'.killed'))
        engine._strategies = loader.load_all_approved(self.strategies)
        engine._positions_prev = [_position('G',71),_position('SPY',2)]
        engine._positions = [_position('SPY',2)]
        engine._position_visibility_valid = True
        engine._active_protective_stops = {'G':self.record}
        engine._git_commit_stopped_out_strategy = lambda **kwargs: 'fixture-commit'
        return engine

    async def test_close_persists_actual_price_once_and_preserves_spy(self):
        await self.connector.connect()
        self.connector.executions_history = [self.execution]
        engine = self.engine()
        await engine._detect_strategy_stop_outs(cycle_id='test')
        history = read_stop_history(self.journal)
        self.assertEqual([(p.ticker,p.qty) for p in _positions_from_journal(history)],[('SPY',2)])
        self.assertIn("stopped_out_fill_price: '29.77'",self.path.read_text())
        self.assertEqual(len([r for r in history if r['event_type']=='order_filled']),1)
        await engine._journal_verified_stop_close(self.record,71)
        self.assertEqual(len([r for r in read_stop_history(self.journal) if r['event_type']=='order_filled']),1)

    async def test_missing_execution_halts_without_any_lifecycle_change(self):
        await self.connector.connect()
        engine = self.engine()
        await engine._detect_strategy_stop_outs(cycle_id='test')
        self.assertEqual(engine.state,EngineState.HALTED)
        self.assertIn('status: approved',self.path.read_text())
        self.assertTrue(unresolved_stop_barriers(read_stop_history(self.journal)))
        self.assertEqual(len(self.connector.submitted_orders),0)
        restarted = self.engine()
        with patch.dict('os.environ',{'K2BI_ALLOW_RECOVERY_MISMATCH':'1'}):
            result = await restarted.tick_once()
        self.assertEqual(result.state_after,EngineState.HALTED)
        self.assertEqual(result.orders_submitted,0)

    async def test_lifecycle_crash_after_fill_resumes_without_duplicate_sell(self):
        await self.connector.connect()
        self.connector.executions_history = [self.execution]
        first = self.engine()
        first._flip_strategy_to_stopped_out = AsyncMock(side_effect=RuntimeError('injected'))
        with self.assertRaises(RuntimeError):
            await first._detect_strategy_stop_outs(cycle_id='test')
        self.assertEqual(first.state,EngineState.HALTED)
        second = self.engine()
        await second._resume_verified_stop_lifecycle(read_stop_history(self.journal))
        self.assertIn('status: stopped_out',self.path.read_text())
        third = self.engine()
        await third._resume_verified_stop_lifecycle(read_stop_history(self.journal))
        history = read_stop_history(self.journal)
        self.assertEqual(len([r for r in history if r['event_type']=='order_filled']),1)
        self.assertEqual(len([r for r in history if r['event_type']=='strategy_stopped_out']),1)

    async def test_file_written_commit_failed_cannot_silently_complete(self):
        await self.connector.connect()
        self.connector.executions_history = [self.execution]
        first = self.engine()
        def fail(**kwargs): raise RuntimeError('commit failure')
        first._git_commit_stopped_out_strategy = fail
        with self.assertRaises(RuntimeError):
            await first._detect_strategy_stop_outs(cycle_id='test')
        restarted = self.engine()
        with self.assertRaises(ValueError):
            await restarted._resume_verified_stop_lifecycle(read_stop_history(self.journal))
        self.assertEqual(len(self.connector.submitted_orders),0)

    def test_restores_owned_stop_and_rejects_ambiguity(self):
        engine = self.engine();engine._positions = [_position('G',71),_position('SPY',2)]
        order = BrokerOpenOrder('1001','2000001','G','sell',71,0,Decimal('0'),
            'Submitted',self.now,client_tag=f'k2bi:{STRATEGY_ID}:T-restart:stop',
            aux_price=Decimal('30'),order_type='STP')
        engine._restore_active_stops(self.journal.read_all(),[order],[])
        self.assertEqual(engine._active_protective_stops['G'].parent_trade_id,'T-restart')
        self.assertEqual(engine._active_protective_stops['G'].quantity,71)
        self.assertNotIn('SPY',engine._active_protective_stops)
        with self.assertRaises(ValueError):
            engine._restore_active_stops(self.journal.read_all(),[order,order],[])
        from dataclasses import replace
        for fields in [{'order_type':'STP LMT'},{'side':'buy'},{'ticker':'SPY'},
                       {'qty':70},{'filled_qty':1},{'aux_price':Decimal('29')},
                       {'broker_perm_id':''},{'broker_order_id':'0'}]:
            with self.subTest(fields=fields),self.assertRaises(ValueError):
                engine._restore_active_stops(self.journal.read_all(),[replace(order,**fields)],[])

    async def test_rotated_barrier_does_not_expire(self):
        self.journal.append('engine_stopped',ticker='G',broker_perm_id='2000001',
            payload={'reason':'protective_stop_fill_unverified','quantity':71,
                     'parent_trade_id':'T-restart'},ts=self.now-timedelta(days=45))
        engine = self.engine()
        result=await engine.tick_once()
        self.assertEqual(result.state_after,EngineState.HALTED)
        self.assertEqual(result.orders_submitted,0)

    async def test_same_tick_disappearance_blocks_submission(self):
        engine=self.engine();engine.state=EngineState.CONNECTED_IDLE
        engine._active_protective_stops={}
        engine._refresh_positions_at_cycle_top=AsyncMock(return_value=True)
        engine._eod_due=lambda:False
        from execution.engine.main import TickResult
        result=TickResult(state_before=engine.state,state_after=engine.state)
        await engine._run_tick_body(result)
        self.assertEqual(engine.state,EngineState.HALTED)
        self.assertEqual(result.orders_submitted,0)
        self.assertEqual(self.connector.submitted_orders,[])

    async def test_full_startup_restores_stop_then_close_survives_restart(self):
        self.connector.positions = [_position('G',71),_position('SPY',2)]
        self.connector.open_orders = [BrokerOpenOrder('1001','2000001','G','sell',71,0,
            Decimal('0'),'Submitted',self.now,client_tag=f'k2bi:{STRATEGY_ID}:T-restart:stop',
            aux_price=Decimal('30'),order_type='STP')]
        first=self.engine()
        result=await first.tick_once()
        self.assertEqual(result.state_after,EngineState.CONNECTED_IDLE)
        self.assertEqual(first._active_protective_stops['G'].stop_perm_id,2000001)
        self.connector.executions_history=[self.execution]
        self.connector.positions=[_position('SPY',2)];self.connector.open_orders=[]
        first._positions_prev=first._positions
        first._positions=list(self.connector.positions)
        await first._detect_strategy_stop_outs(cycle_id='full-restart')
        second=self.engine()
        result=await second.tick_once()
        self.assertEqual(result.state_after,EngineState.CONNECTED_IDLE)
        self.assertEqual([(p.ticker,p.qty) for p in second._positions],[('SPY',2)])
        self.assertEqual(second._strategies,[])
        self.assertEqual(result.orders_submitted,0)

    async def test_execution_timeout_and_wrong_identity_halt(self):
        from dataclasses import replace
        await self.connector.connect()
        for execution in [replace(self.execution,qty=1),
                          replace(self.execution,ticker='SPY'),
                          replace(self.execution,broker_order_id='2')]:
            with self.subTest(execution=execution):
                self.connector.executions_history=[execution]
                engine=self.engine()
                await engine._detect_strategy_stop_outs(cycle_id='bad-exec')
                self.assertEqual(engine.state,EngineState.HALTED)
        engine=self.engine()
        self.connector.get_executions_since=AsyncMock(side_effect=TimeoutError())
        await engine._detect_strategy_stop_outs(cycle_id='timeout')
        self.assertEqual(engine.state,EngineState.HALTED)
        self.assertEqual([r for r in self.journal.read_all() if r['event_type']=='order_filled'],[])
