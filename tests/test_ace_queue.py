import os
import queue
import sys
import types
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), '..', 'klipper', 'extras'))

if 'serial' not in sys.modules:
    serial = types.ModuleType('serial')

    class SerialException(Exception):
        pass

    serial.SerialException = SerialException
    serial.serialutil = types.SimpleNamespace(SerialException=SerialException)
    sys.modules['serial'] = serial

from ace import AceException, BunnyAce


class FakeReactor:
    def __init__(self):
        self.now = 0.
        self.callbacks = []

    def monotonic(self):
        return self.now

    def pause(self, waketime):
        self.now = waketime

    def register_async_callback(self, callback):
        self.callbacks.append(callback)

    def run_all(self):
        while self.callbacks:
            callback = self.callbacks.pop(0)
            callback(self.now)


class FakeGCode:
    def __init__(self):
        self.messages = []

    def respond_raw(self, message):
        self.messages.append(message)


class FakePrinter:
    def __init__(self):
        self.events = []

    def send_event(self, *event):
        self.events.append(event)


def make_ace():
    ace = BunnyAce.__new__(BunnyAce)
    ace.reactor = FakeReactor()
    ace.gcode = FakeGCode()
    ace.printer = FakePrinter()
    ace.operation_timeout = 1.
    ace._connected = True
    ace._info = {
        'status': 'ready',
        'temp': 0,
        'dryer_status': {'status': 'stop', 'target_temp': 0,
                          'duration': 0, 'remain_time': 0},
        'slots': [
            {'index': i, 'status': 'empty', 'sku': '', 'type': '',
             'rfid': 0, 'brand': '', 'color': [0, 0, 0]}
            for i in range(4)
        ],
    }
    ace._callback_map = {}
    ace._op_queue = queue.Queue()
    ace._op_running = False
    ace._op_worker_scheduled = False
    ace._last_operation_error = None
    ace._feed_assist_index = -1
    ace._desired_feed_assist_index = -1
    ace._assist_reconcile_scheduled = False
    ace.extruder_gate_map = [0, 1, 2, 3]
    ace._gate_preloaded = [False, False, False, False]
    ace.feed_length = 600
    ace.feed_speed = 50
    ace.retract_length = 100
    ace.retract_speed = 20
    return ace


class AceQueueTests(unittest.TestCase):
    def test_extruder_gate_map_configuration(self):
        self.assertEqual(
            [2, None, 0, None],
            BunnyAce._parse_extruder_gate_map('0=2, 2=0'))
        self.assertEqual(
            [None, None, None, None],
            BunnyAce._parse_extruder_gate_map('none'))
        with self.assertRaises(ValueError):
            BunnyAce._parse_extruder_gate_map('0=4')
        with self.assertRaises(ValueError):
            BunnyAce._parse_extruder_gate_map('0=invalid')
        with self.assertRaises(ValueError):
            BunnyAce._parse_extruder_gate_map('0=1, 2=1')

    def test_extruder_gate_map_lookups_both_directions(self):
        ace = make_ace()
        ace.extruder_gate_map = [2, None, 0, None]

        self.assertEqual(2, ace.gate_for_extruder(0))
        self.assertIsNone(ace.gate_for_extruder(1))
        self.assertEqual(2, ace.extruder_for_gate(0))
        self.assertIsNone(ace.extruder_for_gate(1))

    def test_status_is_json_safe_and_does_not_publish_internal_map(self):
        ace = make_ace()
        ace.extruder_gate_map = [2, None, 0, None]
        ace._info.update({
            'temp': 25,
            'dryer_status': {'profiles': {0: {'steps': {1: 50}}}},
        })
        ace.gate_status = [0, 0, 0, 0]

        status = ace.get_status()

        self.assertNotIn('extruder_gate_map', status)
        self.assertEqual(
            {'0': {'steps': {'1': 50}}},
            status['dryer_status']['profiles'])

    def test_status_exposes_gate_slots_and_extruder_mapping(self):
        ace = make_ace()
        ace.extruder_gate_map = [2, None, 0, None]
        ace._info['slots'][0]['status'] = 'ready'
        ace._info['slots'][0]['type'] = 'PLA'
        ace._info['slots'][0]['brand'] = 'Generic'
        ace._info['slots'][0]['rfid'] = 2
        ace._info['slots'][0]['color'] = [255, 0, 0]
        ace.gate_status = [1, 0, 0, 0]

        status = ace.get_status()

        self.assertEqual([1, 0, 0, 0], status['gate_status'])
        self.assertEqual([2, None, 0, None], status['gate_extruder'])
        self.assertEqual('PLA', status['slots'][0]['type'])
        self.assertEqual(2, status['slots'][0]['rfid'])
        self.assertEqual([255, 0, 0], status['slots'][0]['color'])

    def test_status_reports_actual_serial_connection_state(self):
        ace = make_ace()
        ace.gate_status = [0, 0, 0, 0]

        ace._connected = False
        self.assertFalse(ace.get_status()['connected'])

        ace._connected = True
        self.assertTrue(ace.get_status()['connected'])

    def test_non_string_key_diagnostic_reports_nested_path(self):
        value = {'valid': [{2: {'nested': True}}]}

        self.assertEqual(
            ["$.valid[0]: int key 2"],
            BunnyAce._find_non_string_keys(value))

    def test_feed_assist_requests_coalesce_to_latest_gate(self):
        ace = make_ace()
        calls = []

        def enable(index):
            calls.append(('enable', index))
            ace._feed_assist_index = index

        ace._enable_feed_assist_impl = enable
        ace._enable_feed_assist(1)
        ace._enable_feed_assist(3)

        ace.reactor.run_all()

        self.assertEqual([('enable', 3)], calls)
        self.assertEqual(3, ace._feed_assist_index)

    def test_gate_change_disables_old_gate_before_enabling_new_gate(self):
        ace = make_ace()
        ace._feed_assist_index = 0
        ace._desired_feed_assist_index = 0
        calls = []

        def disable(index=-1):
            calls.append(('disable', ace._feed_assist_index))
            ace._feed_assist_index = -1

        def enable(index):
            calls.append(('enable', index))
            ace._feed_assist_index = index

        ace._disable_feed_assist_impl = disable
        ace._enable_feed_assist_impl = enable
        ace._enable_feed_assist(2)

        ace.reactor.run_all()

        self.assertEqual([('disable', 0), ('enable', 2)], calls)

    def test_disable_is_idempotent_when_no_gate_is_active(self):
        ace = make_ace()
        calls = []
        ace._disable_feed_assist_impl = lambda index=-1: calls.append(index)

        ace._disable_feed_assist()
        ace.reactor.run_all()

        self.assertEqual([], calls)

    def test_print_stop_disables_active_feed_assist(self):
        ace = make_ace()
        ace._feed_assist_index = 2
        ace._desired_feed_assist_index = 2
        calls = []

        def disable(index=-1):
            calls.append(ace._feed_assist_index)
            ace._feed_assist_index = -1

        ace._disable_feed_assist_impl = disable

        ace._handle_print_stop()
        ace.reactor.run_all()

        self.assertEqual([2], calls)
        self.assertEqual(-1, ace._desired_feed_assist_index)
        self.assertEqual(-1, ace._feed_assist_index)

    def test_handle_printing_accepts_klipper_event_argument(self):
        # idle_timeout:printing is fired by Klipper with a print_time
        # argument (see extras/idle_timeout.py's handle_sync_print_time) -
        # a handler that only accepts `self` raises a TypeError that takes
        # klippy down mid-print (regression: 2026-09-10).
        ace = make_ace()
        ace.extruder_gate_map = [None, None, None, None]
        ace.toolhead = types.SimpleNamespace(
            get_extruder=lambda: types.SimpleNamespace(extruder_num=0))

        ace._handle_printing(123.456)  # must not raise

    def test_sync_feed_assist_enables_for_mapped_active_extruder(self):
        ace = make_ace()
        ace.extruder_gate_map = [2, None, 0, None]
        ace.toolhead = types.SimpleNamespace(
            get_extruder=lambda: types.SimpleNamespace(extruder_num=0))
        calls = []
        ace._enable_feed_assist_impl = lambda index: (
            calls.append(('enable', index)),
            setattr(ace, '_feed_assist_index', index))

        ace._sync_feed_assist_to_active_extruder()
        ace.reactor.run_all()

        self.assertEqual([('enable', 2)], calls)
        self.assertEqual(2, ace._feed_assist_index)

    def test_sync_feed_assist_disables_for_unmapped_active_extruder(self):
        ace = make_ace()
        ace.extruder_gate_map = [2, None, 0, None]
        ace._feed_assist_index = 2
        ace._desired_feed_assist_index = 2
        ace.toolhead = types.SimpleNamespace(
            get_extruder=lambda: types.SimpleNamespace(extruder_num=1))
        calls = []
        ace._disable_feed_assist_impl = lambda index=-1: (
            calls.append(('disable', ace._feed_assist_index)),
            setattr(ace, '_feed_assist_index', -1))

        ace._sync_feed_assist_to_active_extruder()
        ace.reactor.run_all()

        self.assertEqual([('disable', 2)], calls)
        self.assertEqual(-1, ace._feed_assist_index)

    def test_sync_feed_assist_leaves_state_alone_without_extruder_num(self):
        # A transient toolhead extruder (e.g. mid-probing) with no
        # extruder_num attribute is ambiguous, not "unmapped" - regression
        # guard for a bug where this incorrectly force-disabled feed assist
        # during bed-leveling/calibration mid-print (2026-09-10).
        ace = make_ace()
        ace.extruder_gate_map = [2, None, 0, None]
        ace._feed_assist_index = 2
        ace._desired_feed_assist_index = 2
        ace.toolhead = types.SimpleNamespace(
            get_extruder=lambda: types.SimpleNamespace(name='probe'))
        calls = []
        ace._disable_feed_assist_impl = lambda index=-1: calls.append('disable')
        ace._enable_feed_assist_impl = lambda index: calls.append('enable')

        ace._sync_feed_assist_to_active_extruder()
        ace.reactor.run_all()

        self.assertEqual([], calls)
        self.assertEqual(2, ace._feed_assist_index)
        self.assertEqual(2, ace._desired_feed_assist_index)

    def test_retract_runs_after_previously_queued_disable(self):
        ace = make_ace()
        ace._feed_assist_index = 1
        ace._desired_feed_assist_index = 1
        ace._gate_preloaded[1] = True
        calls = []

        def disable(index=-1):
            calls.append(('disable', ace._feed_assist_index))
            ace._feed_assist_index = -1

        def retract(index, length, speed):
            calls.append(('retract', index, length, speed))

        ace._disable_feed_assist_impl = disable
        ace._retract_impl = retract
        ace._disable_feed_assist()
        ace.retract_fil(1)

        ace.reactor.run_all()

        self.assertEqual(
            [('disable', 1), ('retract', 1, 100, 20)], calls)
        self.assertFalse(ace._gate_preloaded[1])

    def test_prepare_gate_for_load_only_feeds_after_retract(self):
        ace = make_ace()
        calls = []
        ace._feed = lambda index, length, speed, how_wait=None: calls.append(
            (index, length, speed, how_wait))

        ace.prepare_gate_for_load(2)
        ace.prepare_gate_for_load(2)

        self.assertEqual([(2, 600, 50, 0)], calls)
        self.assertTrue(ace._gate_preloaded[2])

    def test_queue_continues_after_failed_job(self):
        ace = make_ace()
        calls = []

        def fail():
            raise RuntimeError('expected failure')

        ace._enqueue_ace_job(fail, _job_name='failing job')
        ace._enqueue_ace_job(
            lambda: calls.append('completed'), _job_name='next job')

        ace.reactor.run_all()

        self.assertEqual(['completed'], calls)
        self.assertEqual('expected failure', ace._last_operation_error)
        self.assertEqual(
            ('ace:operation_error', 'failing job', 'expected failure'),
            ace.printer.events[0])

    def test_wait_ace_ready_times_out(self):
        ace = make_ace()
        ace._info['status'] = 'busy'
        ace.operation_timeout = 0.2

        with self.assertRaises(AceException):
            ace.wait_ace_ready()


if __name__ == '__main__':
    unittest.main()
