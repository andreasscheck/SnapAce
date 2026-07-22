import logging
import json
import struct
import queue
import traceback
import serial
from serial import SerialException


class AceException(Exception):
    pass


GATE_UNKNOWN = -1
GATE_EMPTY = 0
GATE_AVAILABLE = 1  # Available to load from either buffer or spool


class BunnyAce:
    VARS_ACE_REVISION = 'ace__revision'

    def __init__(self, config):
        self._connected = False
        self._serial = None
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self._name = config.get_name()
        self.send_time = None
        self.ace_dev_fd = None
        self.heartbeat_timer = None

        self.gate_status = [GATE_UNKNOWN, GATE_UNKNOWN, GATE_UNKNOWN, GATE_UNKNOWN]
        self.read_buffer = bytearray()
        if self._name.startswith('ace '):
            self._name = self._name[4:]

        self.save_variables = self.printer.lookup_object('save_variables', None)
        if self.save_variables:
            revision_var = self.save_variables.allVariables.get(self.VARS_ACE_REVISION, None)
            if revision_var is None:
                config.error("You have custom [save_variables]. "
                             "Copy the contents of ace_vars.cfg to your file and remove [save_variables] in ace.cfg")
        else:
            config.error("There is no [save_variables] in the config. Check installation guide")


        self.serial_id = config.get('serial', '/dev/serial/by-id/usb-ANYCUBIC_ACE_1-if00')
        self.baud = config.getint('baud', 115200)

        self.feed_speed = config.getint('feed_speed', 50)
        self.retract_speed = config.getint('retract_speed', 50)
        self.retract_length = config.getint('retract_length', 100)
        self.feed_length = config.getint('feed_length', 100)
        self.max_dryer_temperature = config.getint('max_dryer_temperature', 55)
        self.operation_timeout = config.getfloat(
            'operation_timeout', 30., above=0.)
        try:
            self.extruder_gate_map = self._parse_extruder_gate_map(
                config.get(
                    'extruder_gate_map', '0=0, 1=1, 2=2, 3=3'))
        except ValueError as e:
            raise config.error(str(e))

        self._callback_map = {}
        self._feed_assist_index = -1
        self._request_id = 0
        self._op_queue = queue.Queue()
        self._op_running = False
        self._op_worker_scheduled = False
        self._last_operation_error = None
        self._desired_feed_assist_index = -1
        self._assist_reconcile_scheduled = False

        # Default data to prevent exceptions
        self._info = {
            'status': 'ready',
            'dryer_status': {
                'status': 'stop',
                'target_temp': 0,
                'duration': 0,
                'remain_time': 0
            },
            'temp': 0,
            'enable_rfid': 1,
            'fan_speed': 7000,
            'feed_assist_count': 0,
            'cont_assist_time': 0.0,
            'slots': [
                {
                    'index': 0,
                    'status': 'empty1',
                    'sku': '',
                    'type': '',
                    'rfid': 0,
                    'brand':'',
                    'color': [0, 0, 0]
                },
                {
                    'index': 1,
                    'status': 'empty1',
                    'sku': '',
                    'type': '',
                    'rfid': 0,
                    'brand': '',
                    'color': [0, 0, 0]
                },
                {
                    'index': 2,
                    'status': 'empty1',
                    'sku': '',
                    'type': '',
                    'rfid': 0,
                    'brand': '',
                    'color': [0, 0, 0]
                },
                {
                    'index': 3,
                    'status': 'empty1',
                    'sku': '',
                    'type': '',
                    'rfid': 0,
                    'brand': '',
                    'color': [0, 0, 0]
                }
            ]
        }
        self.extruder_sensor = None

        self.printer.register_event_handler('klippy:ready', self._handle_ready)
        self.printer.register_event_handler('klippy:disconnect', self._handle_disconnect)

        self.gcode.register_command(
            'ACE_START_DRYING', self.cmd_ACE_START_DRYING,
            desc=self.cmd_ACE_START_DRYING_help)
        self.gcode.register_command(
            'ACE_STOP_DRYING', self.cmd_ACE_STOP_DRYING,
            desc=self.cmd_ACE_STOP_DRYING_help)
        self.gcode.register_command(
            'ACE_ENABLE_FEED_ASSIST', self.cmd_ACE_ENABLE_FEED_ASSIST,
            desc=self.cmd_ACE_ENABLE_FEED_ASSIST_help)
        self.gcode.register_command(
            'ACE_DISABLE_FEED_ASSIST', self.cmd_ACE_DISABLE_FEED_ASSIST,
            desc=self.cmd_ACE_DISABLE_FEED_ASSIST_help)
        self.gcode.register_command(
            'ACE_FEED', self.cmd_ACE_FEED,
            desc=self.cmd_ACE_FEED_help)
        self.gcode.register_command(
            'ACE_RETRACT', self.cmd_ACE_RETRACT,
            desc=self.cmd_ACE_RETRACT_help)
        self.gcode.register_command(
            'ACE_CHECK_JSON_STATUS', self.cmd_ACE_CHECK_JSON_STATUS,
            desc=self.cmd_ACE_CHECK_JSON_STATUS_help)

    def _handle_ready(self):
        self.toolhead = self.printer.lookup_object('toolhead')

        logging.info(f'ACE: Connecting to {self.serial_id}')
        # We can catch timing where ACE reboots itself when no data is available from host. We're avoiding it with this hack
        self._connected = False
        self._queue = queue.Queue()
        self.connect_timer = self.reactor.register_timer(self._connect, self.reactor.NOW)

    def _handle_disconnect(self):
        logging.info(f'ACE: Closing connection to {self.serial_id}')
        self._serial_disconnect()
        self._queue = None

    def _color_message(self, msg):
        try:
            html_msg = msg.format(
                '</span>',  # {0}
                '<span style="color:#FFFF00">',  # {1}
                '<span style="color:#90EE90">',  # {2}
                '<span style="color:#458EFF">',  # {3}
                '<b>',  # {5}
                '</b>'  # {6}
            )
        except (IndexError, KeyError, ValueError) as e:
            html_msg = msg
        return html_msg

    def log_warning(self, msg):
        c_msg = self._color_message(f'{{1}}{msg}{{0}}')
        self.gcode.respond_raw(c_msg)

    def log_always(self, msg: str, color=False):
        c_msg = self._color_message(msg) if color else msg
        self.gcode.respond_raw(c_msg)

    def log_error(self, msg):
        self.error_msg = msg
        self.gcode.respond_raw(f"!! {msg}")

    def save_variable(self, variable, value, write=False):
        self.save_variables.allVariables[variable] = value
        if write:
            self.write_variables()

    def rgb2hex(self, r, g, b):
        return "%02X%02X%02X" % (r, g, b)

    def delete_variable(self, variable, write=False):
        _ = self.save_variables.allVariables.pop(variable, None)
        if write:
            self.write_variables()

    def write_variables(self):
        mmu_vars_revision = self.save_variables.allVariables.get(self.VARS_ACE_REVISION, 0) + 1
        self.gcode.run_script_from_command(
            f"SAVE_VARIABLE VARIABLE={self.VARS_ACE_REVISION} VALUE={mmu_vars_revision}")

    def _get_next_request_id(self) -> int:
        self._request_id += 1
        if self._request_id >= 300000:
            self._request_id = 0
        return self._request_id

    def _serial_disconnect(self):
        if self._serial is not None and self._serial.is_open:
            self._serial.close()
        self._connected = False
        self._feed_assist_index = -1
        self._callback_map.clear()
        self._cancel_pending_jobs('ACE disconnected')
        if self.heartbeat_timer:
            self.reactor.unregister_timer(self.heartbeat_timer)
        if self.ace_dev_fd:
            self.reactor.set_fd_wake(self.ace_dev_fd, False, False)
            self.ace_dev_fd = None

    def _connect(self, eventtime):
        self.log_always('Try connecting25')

        def info_callback(self, response):
            if response.get('msg') != 'success':
                self.log_error(f"ACE Error: {response.get('msg')}")
            result = response.get('result', {})
            model = result.get('model', 'Unknown')
            firmware = result.get('firmware', 'Unknown')
            self.log_always(f"{{2}}ACE: Connected2 to {model} {{0}} \n Firmware Version: {{3}}{firmware}{{0}}", True)

        try:
            self._serial = serial.Serial(
                port=self.serial_id,
                baudrate=self.baud,
                exclusive=True,
                rtscts=True,
                timeout=0,
                write_timeout=0)

            if self._serial.is_open:
                self._connected = True
                self._request_id = 0
                logging.info(f'ACE: Connected to {self.serial_id}')
                self.ace_dev_fd = self.reactor.register_fd(
                    self._serial.fileno(),
                    self._reader_cb,
                )
                self.heartbeat_timer = self.reactor.register_timer(self._periodic_heartbeat_event, self.reactor.NOW)
                self.send_request(request={"method": "get_info"},
                                  callback=lambda self, response: info_callback(self, response))
                if self._desired_feed_assist_index != -1:
                    self._schedule_feed_assist_reconcile()
                self.reactor.unregister_timer(self.connect_timer)
                return self.reactor.NEVER
        except serial.serialutil.SerialException:
            self._serial = None
            logging.info('ACE: Conn error')
            self.log_error(f'Error connecting to {self.serial_id}')
        except Exception as e:
            self.log_error(f"ACE Error: {e}")

        return eventtime + 1

    def _calc_crc(self, buffer):
        _crc = 0xFFFF
        for byte in buffer:
            data = byte
            data ^= _crc & 0xFF
            data ^= (data & 0x0F) << 4
            _crc = ((data << 8) | (_crc >> 8)) ^ (data >> 4) ^ (data << 3)
        return _crc

    def _send_request(self, request):
        if 'id' not in request:
            request['id'] = self._get_next_request_id()

        payload = json.dumps(request).encode('utf-8')
        if len(payload) > 1024:
            logging.error(f"ACE: Payload too large ({len(payload)} bytes)")
            return

        crc = self._calc_crc(payload)
        # Re-generate payload if CRC matches sync bytes 0xFFAA to prevent freezing
        # as suggested by protocol description.
        attempts = 0
        while crc == 0xAAFF and attempts < 10:
            request['id'] = self._get_next_request_id()
            payload = json.dumps(request).encode('utf-8')
            crc = self._calc_crc(payload)
            attempts += 1

        data = bytes([0xFF, 0xAA])
        data += struct.pack('<H', len(payload))
        data += payload
        data += struct.pack('<H', crc)
        data += bytes([0xFE])
        try:
            self._serial.write(data)
        except Exception:
            self.log_error("ACE: Error writing to serial")
            self.log_warning("Try reconnecting")
            self._serial_disconnect()
            self.connect_timer = self.reactor.register_timer(self._connect, self.reactor.NOW)

    def _pre_load(self, gate):
        if self.extruder_for_gate(gate) is None:
            return
        self.log_always('Wait ACE preload')
        self.wait_ace_ready()
        self._feed(gate, self.feed_length, self.feed_speed, 0)
        self.log_always("Select AutoLoad from the menu")

    def _periodic_heartbeat_event(self, eventtime):
        def callback(self, response):
            if response is not None:
                for i in range(4):
                    extruder_index = self.extruder_for_gate(i)
                    if (extruder_index is not None and
                            self.gate_status[i] == GATE_EMPTY and
                            response['result']['slots'][i]['status'] != 'empty'):
                        self.log_always('auto_feed')
                        self.reactor.register_async_callback(
                            (lambda et, c=self._pre_load, gate=i: c(gate)))

                    if (extruder_index is not None and
                            response['result']['slots'][i]['rfid'] == 2 and
                            self._info['slots'][i]['rfid'] != 2):
                        self.log_always('find_rfid')
                        spool_inf = response['result']['slots'][i]
                        self.log_always(str(spool_inf))
                        self.gcode.run_script_from_command(f'SET_PRINT_FILAMENT_CONFIG '
                                                           f'CONFIG_EXTRUDER={extruder_index} '
                                                           f'FILAMENT_TYPE="{spool_inf.get("type", "PLA")}" '                                               
                                                           f'FILAMENT_COLOR_RGBA={self.rgb2hex(*spool_inf.get("color", (0,0,0)))} '
                                                           f'VENDOR="{spool_inf.get("brand", "Generic")}" '
                                                           f'FILAMENT_SUBTYPE=""')
                    self.gate_status[i] = GATE_EMPTY if response['result']['slots'][i]['status'] == 'empty' \
                        else GATE_AVAILABLE
                self._info = response['result']


        self.send_request({"method": "get_status"}, callback)
        return eventtime + 1

    def _reader_cb(self, eventtime):
        try:
            if self._serial.in_waiting:
                raw_bytes = self._serial.read(size=self._serial.in_waiting)
                self._process_data(raw_bytes)
        except Exception:
            logging.info(f'ACE error reading/processing: {traceback.format_exc()}')
            self.log_error("Unable to communicate with the ACE PRO")
            self.log_warning("Try reconnecting")
            self._serial_disconnect()
            self.connect_timer = self.reactor.register_timer(self._connect, self.reactor.NOW)

    def _process_data(self, raw_bytes):
        self.read_buffer += raw_bytes
        while len(self.read_buffer) >= 7:
            # Find sync bytes
            start = self.read_buffer.find(b'\xFF\xAA')
            if start < 0:
                # No sync bytes found, but we might have partial sync at the end
                if self.read_buffer.endswith(b'\xFF'):
                    self.read_buffer = self.read_buffer[-1:]
                else:
                    self.read_buffer = bytearray()
                break

            if start > 0:
                self.read_buffer = self.read_buffer[start:]

            if len(self.read_buffer) < 4:
                break

            payload_len = struct.unpack('<H', self.read_buffer[2:4])[0]
            if payload_len > 2048:  # Sanity check: protocol says max 1024, but allow some headroom
                self.gcode.respond_info(f"ACE: Invalid payload length {payload_len}, dropping sync bytes")
                self.read_buffer = self.read_buffer[2:]
                continue

            total_len = 4 + payload_len + 2 + 1  # head + payload + crc + tail

            if len(self.read_buffer) < total_len:
                break

            packet = self.read_buffer[:total_len]
            payload = packet[4:4 + payload_len]
            crc_data = packet[4 + payload_len:4 + payload_len + 2]
            tail = packet[-1]

            # if tail != 0xFE:
            #     self.gcode.respond_info(f"Invalid tail byte from ACE: {tail:02X}, dropping sync bytes")
            #     self.read_buffer = self.read_buffer[2:]  # Drop current sync bytes and continue searching
            #     continue
            #
            # calc_crc = struct.pack('<H', self._calc_crc(payload))
            # if crc_data != calc_crc:
            #     self.gcode.respond_info('Invalid CRC from ACE PRO, dropping sync bytes')
            #     self.read_buffer = self.read_buffer[2:]  # Drop current sync bytes and continue searching
            #     continue

            # Packet is valid, consume it from buffer
            self.read_buffer = self.read_buffer[total_len:]

            try:
                ret = json.loads(payload.decode('utf-8'))
            except (json.decoder.JSONDecodeError, UnicodeDecodeError):
                self.log_error('Invalid JSON/UTF-8 from ACE PRO')
                continue

            msg_id = ret.get('id')
            if msg_id in self._callback_map:
                callback = self._callback_map.pop(msg_id)
                callback(self=self, response=ret)

    def send_request(self, request, callback):
        self._info['status'] = 'busy'
        msg_id = self._get_next_request_id()
        self._callback_map[msg_id] = callback
        request['id'] = msg_id
        self._send_request(request)
        return msg_id

    def _wait_until(self, predicate, description, timeout=None):
        if timeout is None:
            timeout = self.operation_timeout
        deadline = self.reactor.monotonic() + timeout
        while not predicate():
            if not self._connected:
                raise AceException(
                    f'ACE disconnected while waiting for {description}')
            now = self.reactor.monotonic()
            if now >= deadline:
                raise AceException(f'ACE timeout while waiting for {description}')
            self.reactor.pause(min(now + 0.1, deadline))

    def wait_ace_ready(self, timeout=None):
        self._wait_until(
            lambda: self._info['status'] == 'ready', 'ready status', timeout)

    def _request_and_wait(self, request, timeout=None):
        result = {'done': False, 'response': None}

        def callback(self, response):
            result['response'] = response
            result['done'] = True

        self.wait_ace_ready(timeout)
        msg_id = self.send_request(request=request, callback=callback)
        try:
            self._wait_until(
                lambda: result['done'], f"response to {request.get('method')}",
                timeout)
        except Exception:
            self._callback_map.pop(msg_id, None)
            raise

        response = result['response'] or {}
        if response.get('code', 0) != 0:
            raise AceException(
                f"ACE {request.get('method')} failed: {response.get('msg')}")
        return response

    def is_ace_ready(self):
        return self._info['status'] == 'ready'

    @staticmethod
    def _parse_extruder_gate_map(value):
        value = value.strip()
        mapping = [None, None, None, None]
        if value.lower() in ('', 'none', 'off'):
            return mapping
        try:
            for item in value.split(','):
                if not item.strip():
                    continue
                extruder, gate = item.split('=', 1)
                extruder = int(extruder.strip())
                gate = int(gate.strip())
                if extruder < 0 or extruder >= len(mapping):
                    raise ValueError(
                        'extruder indexes in extruder_gate_map must be '
                        'between 0 and 3')
                if gate < 0 or gate >= len(mapping):
                    raise ValueError(
                        'ACE gate indexes in extruder_gate_map must be '
                        'between 0 and 3')
                if mapping[extruder] is not None:
                    raise ValueError(
                        f'duplicate extruder index in extruder_gate_map: '
                        f'{extruder}')
                mapping[extruder] = gate
        except (TypeError, ValueError):
            raise ValueError(
                'extruder_gate_map must use EXTRUDER=GATE entries, for '
                'example: 0=2, 2=0')
        assigned_gates = [gate for gate in mapping if gate is not None]
        if len(set(assigned_gates)) != len(assigned_gates):
            raise ValueError(
                'each ACE gate may only be assigned to one extruder')
        return mapping

    def manages_extruder(self, index):
        return self.gate_for_extruder(index) is not None

    def gate_for_extruder(self, index):
        if index < 0 or index >= len(self.extruder_gate_map):
            return None
        return self.extruder_gate_map[index]

    def extruder_for_gate(self, gate):
        for extruder, mapped_gate in enumerate(self.extruder_gate_map):
            if mapped_gate == gate:
                return extruder
        return None

    @staticmethod
    def _json_safe(value):
        """Return a status value accepted by Klipper's strict JSON encoder."""
        if isinstance(value, dict):
            return {
                str(key): BunnyAce._json_safe(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [BunnyAce._json_safe(item) for item in value]
        return value

    @staticmethod
    def _find_non_string_keys(value, path='$'):
        problems = []
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    problems.append(
                        f'{path}: {type(key).__name__} key {key!r}')
                child_path = f'{path}.{key}'
                problems.extend(BunnyAce._find_non_string_keys(
                    item, child_path))
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                problems.extend(BunnyAce._find_non_string_keys(
                    item, f'{path}[{index}]'))
        return problems

    cmd_ACE_CHECK_JSON_STATUS_help = (
        'Checks all printer status objects for non-string dictionary keys')

    def cmd_ACE_CHECK_JSON_STATUS(self, gcmd):
        eventtime = self.reactor.monotonic()
        problems = []
        checked = 0
        for name, obj in self.printer.lookup_objects():
            get_status = getattr(obj, 'get_status', None)
            if get_status is None:
                continue
            try:
                status = get_status(eventtime)
            except Exception as e:
                logging.info(
                    'ACE JSON status check skipped %s: %s', name, e)
                continue
            checked += 1
            for problem in self._find_non_string_keys(status):
                problems.append(f'{name}{problem[1:]}')

        if problems:
            message = 'Non-string JSON dictionary keys: ' + '; '.join(problems)
            logging.error('ACE: %s', message)
            gcmd.respond_info(message)
            return
        message = f'ACE JSON status check: {checked} objects OK'
        logging.info(message)
        gcmd.respond_info(message)

    def dwell(self, delay=1.0):
        curr_ts = self.reactor.monotonic()
        self.reactor.pause(curr_ts + delay)

    def _enqueue_ace_job(self, fn, *args, **kwargs):
        wait = kwargs.pop('_wait', False)
        name = kwargs.pop('_job_name', getattr(fn, '__name__', 'operation'))
        job = {
            'fn': fn,
            'args': args,
            'kwargs': kwargs,
            'name': name,
            'done': False,
            'cancelled': False,
            'error': None,
        }
        self._op_queue.put(job)
        if not self._op_worker_scheduled:
            self._op_worker_scheduled = True
            self.reactor.register_async_callback(self._run_next_ace_job)
        if wait:
            self._wait_for_ace_job(job)
        return job

    def _wait_for_ace_job(self, job):
        try:
            self._wait_until(
                lambda: job['done'], f"queued job {job['name']}",
                self.operation_timeout)
        except Exception:
            job['cancelled'] = True
            raise
        if job['error'] is not None:
            raise AceException(
                f"ACE job {job['name']} failed: {job['error']}")

    def _cancel_pending_jobs(self, reason):
        while True:
            try:
                job = self._op_queue.get_nowait()
            except queue.Empty:
                break
            job['cancelled'] = True
            job['done'] = True
            job['error'] = AceException(reason)
        self._assist_reconcile_scheduled = False

    def _run_next_ace_job(self, eventtime):
        self._op_worker_scheduled = False
        if self._op_running:
            return

        try:
            job = self._op_queue.get_nowait()
        except queue.Empty:
            return

        self._op_running = True
        try:
            if not job['cancelled']:
                job['fn'](*job['args'], **job['kwargs'])
        except Exception as e:
            job['error'] = e
            self._last_operation_error = str(e)
            self.log_error(f"ACE queued job {job['name']} failed: {e}")
            try:
                self.printer.send_event(
                    'ace:operation_error', job['name'], str(e))
            except Exception:
                logging.exception('ACE: unable to publish operation error')
            logging.exception('ACE: queued job %s failed', job['name'])
        finally:
            job['done'] = True
            self._op_running = False

        if not self._op_queue.empty() and not self._op_worker_scheduled:
            self._op_worker_scheduled = True
            self.reactor.register_async_callback(self._run_next_ace_job)

    def _extruder_move(self, length, speed):
        pos = self.toolhead.get_position()
        pos[3] += length
        self.toolhead.move(pos, speed)
        return pos[3]

    cmd_ACE_START_DRYING_help = 'Starts ACE Pro dryer'

    def cmd_ACE_START_DRYING(self, gcmd):
        temperature = gcmd.get_int('TEMP')
        duration = gcmd.get_int('DURATION', 240)

        if duration <= 0:
            raise gcmd.error('Wrong duration')
        if temperature <= 0 or temperature > self.max_dryer_temperature:
            raise gcmd.error('Wrong temperature')

        self._enqueue_ace_job(
            self._start_drying_impl, temperature, duration,
            _wait=True, _job_name='start drying')

    def _start_drying_impl(self, temperature, duration):
        self._request_and_wait({
            "method": "drying",
            "params": {
                "temp": temperature,
                "fan_speed": 7000,
                "duration": duration,
            },
        })
        self.gcode.respond_info('Started ACE drying')

    cmd_ACE_STOP_DRYING_help = 'Stops ACE Pro dryer'

    def cmd_ACE_STOP_DRYING(self, gcmd):
        self._enqueue_ace_job(
            self._stop_drying_impl, _wait=True, _job_name='stop drying')

    def _stop_drying_impl(self):
        self._request_and_wait({"method": "drying_stop"})
        self.gcode.respond_info('Stopped ACE drying')

    def _enable_feed_assist(self, index):
        self._validate_index(index)
        self._desired_feed_assist_index = index
        self._schedule_feed_assist_reconcile()

    def _schedule_feed_assist_reconcile(self):
        if self._assist_reconcile_scheduled:
            return
        self._assist_reconcile_scheduled = True
        self._enqueue_ace_job(
            self._reconcile_feed_assist,
            _job_name='reconcile feed assist')

    def _reconcile_feed_assist(self):
        completed = False
        try:
            while self._feed_assist_index != self._desired_feed_assist_index:
                if self._feed_assist_index != -1:
                    self._disable_feed_assist_impl()
                    continue
                if self._desired_feed_assist_index != -1:
                    self._enable_feed_assist_impl(
                        self._desired_feed_assist_index)
            completed = True
        finally:
            self._assist_reconcile_scheduled = False
            if completed and (
                    self._feed_assist_index != self._desired_feed_assist_index):
                self._schedule_feed_assist_reconcile()

    def _validate_index(self, index):
        if index < 0 or index >= 4:
            raise AceException(f'Wrong ACE gate index: {index}')

    def _enable_feed_assist_impl(self, index):
        self._validate_index(index)
        if self._feed_assist_index == index:
            return
        if self._feed_assist_index != -1:
            self._disable_feed_assist_impl()
        self._request_and_wait({
            "method": "start_feed_assist",
            "params": {"index": index},
        })
        self._feed_assist_index = index
        self.dwell(delay=0.7)

    cmd_ACE_ENABLE_FEED_ASSIST_help = 'Enables ACE feed assist'

    def cmd_ACE_ENABLE_FEED_ASSIST(self, gcmd):
        index = gcmd.get_int('INDEX')

        if index < 0 or index >= 4:
            raise gcmd.error('Wrong index')

        self._enable_feed_assist(index)

    def _disable_feed_assist(self, index=-1):
        if index != -1:
            self._validate_index(index)
        self._desired_feed_assist_index = -1
        self._schedule_feed_assist_reconcile()

    def _disable_feed_assist_impl(self, index=-1):
        active_index = self._feed_assist_index
        if active_index == -1:
            return
        self._request_and_wait({
            "method": "stop_feed_assist",
            "params": {"index": active_index},
        })
        self._feed_assist_index = -1
        self._retract_impl(active_index, 5, 10)
        self.dwell(0.3)
        self.gcode.respond_info('Disabled ACE feed assist')

    cmd_ACE_DISABLE_FEED_ASSIST_help = 'Disables ACE feed assist'

    def cmd_ACE_DISABLE_FEED_ASSIST(self, gcmd):
        index = gcmd.get_int('INDEX', self._feed_assist_index)

        if index != -1 and (index < 0 or index >= 4):
            raise gcmd.error('Wrong index')

        self._disable_feed_assist(index)

    def _feed(self, index, length, speed, how_wait=None):
        self._validate_index(index)
        return self._enqueue_ace_job(
            self._feed_impl, index, length, speed, how_wait,
            _wait=True, _job_name=f'feed gate {index}')

    def _feed_impl(self, index, length, speed, how_wait=None):
        self._request_and_wait({
            "method": "feed_filament",
            "params": {"index": index, "length": length, "speed": speed},
        })
        if how_wait is not None:
            self.dwell(delay=(how_wait / speed) + 0.1)
        else:
            self.dwell(delay=(length / speed) + 0.1)

    cmd_ACE_FEED_help = 'Feeds filament from ACE'

    def cmd_ACE_FEED(self, gcmd):
        index = gcmd.get_int('INDEX')
        length = gcmd.get_int('LENGTH')
        speed = gcmd.get_int('SPEED', self.feed_speed)

        if index < 0 or index >= 4:
            raise gcmd.error('Wrong index')
        if length <= 0:
            raise gcmd.error('Wrong length')
        if speed <= 0:
            raise gcmd.error('Wrong speed')

        self._feed(index, length, speed)

    def _retract(self, index, length, speed):
        self._validate_index(index)
        return self._enqueue_ace_job(
            self._retract_impl, index, length, speed,
            _wait=True, _job_name=f'retract gate {index}')

    def _retract_impl(self, index, length, speed):
        self._request_and_wait({
            "method": "unwind_filament",
            "params": {"index": index, "length": length, "speed": speed},
        })
        self.dwell(delay=(length / speed) + 0.1)

    def retract_fil(self, index):
        self._validate_index(index)
        self._enqueue_ace_job(
            self._retract_impl, index, self.retract_length,
            self.retract_speed, _job_name=f'retract filament gate {index}')

    cmd_ACE_RETRACT_help = 'Retracts filament back to ACE'

    def cmd_ACE_RETRACT(self, gcmd):
        index = gcmd.get_int('INDEX')
        length = gcmd.get_int('LENGTH')
        speed = gcmd.get_int('SPEED', self.retract_speed)

        if index < 0 or index >= 4:
            raise gcmd.error('Wrong index')
        if length <= 0:
            raise gcmd.error('Wrong length')
        if speed <= 0:
            raise gcmd.error('Wrong speed')

        self._retract(index, length, speed)

    def _set_feeding_speed(self, index, speed):
        self._validate_index(index)
        return self._enqueue_ace_job(
            self._set_feeding_speed_impl, index, speed, _wait=True,
            _job_name=f'update feeding speed for gate {index}')

    def _set_feeding_speed_impl(self, index, speed):
        self._request_and_wait({
            "method": "update_feeding_speed",
            "params": {"index": index, "speed": speed},
        })

    def _stop_feeding(self, index):
        self._validate_index(index)
        return self._enqueue_ace_job(
            self._stop_feeding_impl, index, _wait=True,
            _job_name=f'stop feeding gate {index}')

    def _stop_feeding_impl(self, index):
        self._request_and_wait({
            "method": "stop_feed_filament", "params": {"index": index},
        })

    def get_status(self, eventtime=None):
        status = {
            'status': self._info['status'],
            'temp': self._info['temp'],
            'dryer_status': self._info['dryer_status'],
            'gate_status': self.gate_status,
            'feed_assist_index': self._feed_assist_index,
            'desired_feed_assist_index': self._desired_feed_assist_index,
            'operation_queue_size': self._op_queue.qsize(),
            'last_operation_error': self._last_operation_error,
        }
        # The ACE status partly originates from device responses. Sanitize the
        # complete public structure instead of relying on every nested mapping
        # to already use string keys.
        return self._json_safe(status)


def load_config(config):
    return BunnyAce(config)
