# Copyright 2018-2019 Jetperch LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from joulescope import span
from joulescope.stream_buffer import StreamBuffer, stats_to_api, STATS_FIELDS, STATS_VALUES
import threading
import queue
import numpy as np
import logging


TIMEOUT = 10.0


def to_view_statistics(b, idx, units):
    return {
        'μ': b[:, idx, 0].copy(),
        'σ2': b[:, idx, 1].copy(),
        'min': b[:, idx, 2].copy(),
        'max': b[:, idx, 3].copy(),
        'units': units,
    }


def data_array_to_update(x_limits, x, data_array):
    """Convert raw data buffer to a view update.

    :param x_limits: The list of [x_min, x_max] or None if unknown.
    :param x: The np.ndarray of x-axis times.
    :param data_array: The N x STATS_FIELDS x STATS_VALUES np.ndarray containing:
        current, voltage, power, current_range, current_lsb, voltage_lsb
        mean, variance, minimum, maximum
    """
    return {
        'time': {
            'x': x,
            'limits': x_limits,
            'range': [float(x[0]), float(x[-1])],
            'delta': float(x[-1] - x[0]),
            'units': 's',
        },
        'signals': {
            'current': to_view_statistics(data_array, 0, 'A'),
            'voltage': to_view_statistics(data_array, 1, 'V'),
            'power': to_view_statistics(data_array, 2, 'W'),
            'current_range': to_view_statistics(data_array, 3, ''),
            'current_lsb': to_view_statistics(data_array, 4, ''),
            'voltage_lsb': to_view_statistics(data_array, 5, ''),
        },
        'state': {
            'source_type': 'buffer',  # ['realtime', 'buffer']
        }
    }


class View:

    def __init__(self, stream_buffer, calibration):
        self._state = 'idle'
        self._stream_buffer = None
        self._calibration = calibration
        self._x = None
        self._data = None  # NxMx4 np.float32 [length][current, voltage, power][mean, var, min, max]
        self._x_range = [0.0, 1.0]  # the initial default range
        self._samples_per = 1
        self._data_idx = 0
        self._span = None
        self._changed = True
        self._stream_notify_available = False  # flag when stream_notify called
        self._refresh_requested = False
        self._log = logging.getLogger(__name__)

        self._thread = None
        self._cmd_queue = queue.Queue()  # tuples of (command, args, callback)
        self._response_queue = queue.Queue()
        self.on_update_fn = None  # callable(data)
        self._quit = False
        self.on_close = None  # optional callable() on close

        if stream_buffer is not None:
            self._stream_buffer_assign(stream_buffer)

    def _stream_buffer_assign(self, stream_buffer):
        if self._stream_buffer == stream_buffer:
            return
        self._stream_buffer = stream_buffer
        self._x_range = list(self._stream_buffer.limits_time)  # the initial default range
        length = len(self)
        if length <= 0:
            length = 100
        # todo : investigate - may want inclusive max time (not exclusive) -- off by 1 error?
        self._span = span.Span(limits=self._stream_buffer.limits_time,
                               quant=1.0 / self.sampling_frequency,
                               length=length)

    def __len__(self):
        if self._data is None:
            return 0
        return self._data.shape[0]

    @property
    def sampling_frequency(self):
        if self._stream_buffer is None:
            return None
        return self._stream_buffer.sampling_frequency

    @property
    def calibration(self):
        return self._calibration

    @property
    def limits(self):
        """Get the (x_min, x_max) limits for the view."""
        if self._span is not None:
            return list(self._span.limits)
        return None

    def _cmd_process(self, cmd, args):
        rv = None
        try:
            # self._log.debug('_cmd_process %s - start', cmd)
            if cmd == 'stream_notify':
                rv = self._stream_notify(stream_buffer=args)
            elif cmd == 'refresh':
                if bool(args['force']):
                    self._log.debug('view refresh(force=True) requested')
                    self._update()
                else:
                    self._refresh_requested = True
            elif cmd == 'on_x_change':
                rv = self._on_x_change(*args)
            elif cmd == 'samples_get':
                rv = self._samples_get(**args)
            elif cmd == 'statistics_get':
                rv = self._statistics_get(**args)
            elif cmd == 'start':
                rv = self._start()
            elif cmd == 'stop':
                rv = self._stop()
            elif cmd == 'ping':
                return args
            elif cmd == 'close':
                self._quit = True
            else:
                self._log.warning('unsupported command %s', cmd)
        except:
            self._log.exception('While running command')
        # self._log.debug('_cmd_process %s - done', cmd)
        return rv

    def run(self):
        cmd_count = 0
        timeout = 1.0
        self._log.info('View.run start')
        while not self._quit:
            try:
                cmd, args, cbk = self._cmd_queue.get(timeout=timeout)
            except queue.Empty:
                timeout = 1.0
                if cmd_count and self._refresh_requested and (self._changed or self._stream_notify_available):
                    self._update()
                cmd_count = 0
                continue
            cmd_count += 1
            timeout = 0.0
            rv = self._cmd_process(cmd, args)
            if callable(cbk):
                try:
                    cbk(rv)
                except:
                    self._log.exception('in callback')
        self._log.info('View.run done')

    def _post(self, command, args=None, cbk=None):
        if self._thread is None:
            self._log.info('View._post(%s) when thread not running', command)
        else:
            self._cmd_queue.put((command, args, cbk))

    def _post_block(self, command, args=None, timeout=None):
        timeout = TIMEOUT if timeout is None else float(timeout)
        # self._log.debug('_post_block %s start', command)
        while not self._response_queue.empty():
            self._log.warning('response queue not empty')
            try:
                self._response_queue.get(timeout=0.0)
            except queue.Empty:
                pass
        if self._thread is None:
            raise IOError('View thread not running')
        self._post(command, args, lambda rv_=None: self._response_queue.put(rv_))
        try:
            rv = self._response_queue.get(timeout=timeout)
        except queue.Empty as ex:
            self._log.error('view thread hung: %s - FORCE CLOSE', command)
            self._post('close', None, None)
            self._thread.join(timeout=TIMEOUT)
            self._thread = None
            rv = ex
        except Exception as ex:
            rv = ex
        if isinstance(rv, Exception):
            raise IOError(rv)
        # self._log.debug('_post_block %s done', command)  # rv
        return rv

    def _update_from_buffer(self):
        buffer = self._stream_buffer
        if buffer is None:
            return
        length = len(self)
        data_idx_view_end, sample_id_end, delta = self._view()

        if self._data is None:
            return
        elif not self._changed and 0 == delta:
            return
        elif self._changed or delta >= length:  # perform full recompute
            self._data[:, :, :] = np.nan
            if data_idx_view_end > 0:
                start_idx = (data_idx_view_end - length) * self._samples_per
                # self.log.debug('recompute(start=%s, stop=%s, increment=%s)', start_idx, sample_id_end, self.samples_per)
                buffer.data_get(start_idx, sample_id_end, self._samples_per, self._data)
        elif data_idx_view_end > 0:
            start_idx = self._data_idx * self._samples_per
            # self.log.debug('update(start=%s, stop=%s, increment=%s)', start_idx, sample_id_end, self.samples_per)
            self._data = np.roll(self._data, -delta, axis=0)
            buffer.data_get(start_idx, sample_id_end, self._samples_per, self._data[-delta:, :, :])
        else:
            self._data[:, :, :] = np.nan
        self._data_idx = data_idx_view_end
        self._changed = False

    def _update(self):
        if not callable(self.on_update_fn):
            return
        self._update_from_buffer()
        if self._data is None:
            data = None
        else:
            data = data_array_to_update(self.limits, self._x, self._data)
            if self._state != 'idle':
                data['state']['source_type'] = 'realtime'
        self._stream_notify_available = False
        self._refresh_requested = False
        try:
            self.on_update_fn(data)
        except Exception:
            self._log.exception('in on_update_fn')

    def _clear(self):
        self._changed = True
        self._refresh_requested = True
        self._data_idx = 0
        if self._data is not None:
            self._data[:, :, :] = np.nan

    def _start(self):
        self._log.debug('start')
        self._clear()
        self._state = 'streaming'

    def _stop(self):
        self._log.debug('start')
        self._state = 'idle'

    def _on_x_change(self, cmd, kwargs):
        x_range = list(self._x_range)
        if cmd == 'resize':  # {pixels: int}
            length = kwargs['pixels']
            if length is not None and length != len(self):
                self._log.info('resize %s', length)
                self._span.length = length
                self._data = np.full((length, STATS_FIELDS, STATS_VALUES), np.nan, dtype=np.float32)
                self._changed = True  # invalidate
            x_range, self._samples_per, self._x = self._span.conform_discrete(x_range)
        elif cmd == 'span_absolute':  # {range: (start: float, stop: float)}]
            x_range, self._samples_per, self._x = self._span.conform_discrete(kwargs.get('range'))
        elif cmd == 'span_relative':  # {center: float, gain: float}]
            x_range, self._samples_per, self._x = self._span.conform_discrete(
                x_range, gain=kwargs.get('gain'), pivot=kwargs.get('pivot'))
        elif cmd == 'span_pan':
            delta = kwargs.get('delta', 0.0)
            x_range = [x_range[0] + delta, x_range[-1] + delta]
            x_range, self._samples_per, self._x = self._span.conform_discrete(x_range)
        elif cmd == 'refresh':
            self._log.warning('on_x_change(refresh)')
            self._changed = True
            return
        else:
            self._log.warning('on_x_change(%s) unsupported', cmd)
            return

        if self._state == 'streaming':
            x_max = self._span.limits[1]
            if x_range[1] < x_max:
                x_shift = x_max - x_range[1]
                x_range = [x_range[0] + x_shift, x_max]
            x_range, self._samples_per, self._x = self._span.conform_discrete(x_range)

        self._changed |= (self._x_range != x_range)
        self._clear()
        self._x_range = x_range
        self._log.info('changed=%s, length=%s, span=%s, range=%s, samples_per=%s',
                       self._changed, len(self), self._x_range,
                       self._x_range[1] - self._x_range[0], self._samples_per)
        if self._state == 'idle':
            self._stream_notify(self._stream_buffer)

    def _view(self):
        buffer = self._stream_buffer
        _, sample_id_end = buffer.sample_id_range
        lag_time = self._span.limits[1] - self._x_range[1]
        lag_samples = int(lag_time * self.sampling_frequency) // self._samples_per
        data_idx_stream_end = sample_id_end // self._samples_per
        data_idx_view_end = data_idx_stream_end - lag_samples
        sample_id_end = data_idx_view_end * self._samples_per
        delta = data_idx_view_end - self._data_idx
        return data_idx_view_end, sample_id_end, delta

    def time_to_sample_id(self, t):
        return self._stream_buffer.time_to_sample_id(t)

    def sample_id_to_time(self, s):
        return self._stream_buffer.sample_id_to_time(s)

    def _stream_notify(self, stream_buffer):
        self._stream_buffer = stream_buffer
        self._stream_notify_available = True

    def _convert_time_to_samples(self, x, units):
        if units is None or units == 'seconds':
            return self.time_to_sample_id(x)
        elif units == 'samples':
            return int(x)
        else:
            raise ValueError(f'unsupported units {units}')

    def _convert_time_range_to_samples(self, start, stop, units):
        length = len(self)
        data_idx_view_end, sample_id_end, delta = self._view()
        start_idx = (data_idx_view_end - length) * self._samples_per
        if start is None and units == 'seconds':
            start = start_idx
        else:
            start = self._convert_time_to_samples(start, units)
        if stop is None and units == 'seconds':
            stop = data_idx_view_end
        else:
            stop = self._convert_time_to_samples(stop, units)
        return start, stop

    def _samples_get(self, start=None, stop=None, units=None):
        s1, s2 = self._convert_time_range_to_samples(start, stop, units)
        self._log.debug('_samples_get(start=%r, stop=%r, units=%s) -> %s, %s', start, stop, units, s1, s2)
        data = self._stream_buffer.data_get(start=start, stop=stop)
        return {
            # 'time': {},
            'signals': {
                'current': {
                    'value': data[:, 0, 0],
                    'units': 'A',
                },
                'voltage': {
                    'value': data[:, 1, 0],
                    'units': 'V',
                },
                'power': {
                    'value': data[:, 2, 0],
                    'units': 'W',
                },
                'current_range': {
                    'value': data[:, 3, 0],
                    'units': '',
                },
                'current_lsb': {
                    'value': data[:, 4, 0],
                    'units': '',
                },
                'voltage_lsb': {
                    'value': data[:, 5, 0],
                    'units': '',
                },
                'raw': {
                    'value': self._stream_buffer.raw_get(start=start, stop=stop),
                    'units': 'LSBs',
                },
            },
        }

    def _statistics_get(self, start=None, stop=None, units=None):
        """Get the statistics for the collected sample data over a time range.

        :return: The statistics data structure.  Here is an example:
        """
        s1, s2 = self._convert_time_range_to_samples(start, stop, units)
        # self._log.debug('buffer %s, %s, %s => %s, %s', start, stop, units, s1, s2)
        d = self._stream_buffer.stats_get(start=s1, stop=s2)
        t_start = s1 / self.sampling_frequency
        t_stop = s2 / self.sampling_frequency
        return stats_to_api(d, t_start, t_stop)

    def open(self):
        self.close()
        self._log.info('open')
        self._thread = threading.Thread(name='view', target=self.run)
        self._thread.start()
        self._post_block('ping')
        return

    def start(self, stream_buffer: StreamBuffer):
        self._post_block('start')

    def stop(self):
        if self._thread is not None:
            self._post_block('stop')

    def close(self):
        if self._thread is not None:
            self._log.info('close')
            try:
                self._post_block('close', None)
            except Exception:
                self._log.exception('while attempting to close')
            self._thread.join(timeout=TIMEOUT)
            self._thread = None
            self._data = None
            on_close, self.on_close = self.on_close, None
            if callable(on_close):
                try:
                    on_close()
                except Exception:
                    self._log.exception('view.on_close')
            self._stream_buffer = None

    def refresh(self, force=None):
        return self._post('refresh', {'force': force})

    def on_x_change(self, cmd, kwargs):
        self._post('on_x_change', (cmd, kwargs))

    def stream_notify(self, stream_buffer):
        self._post('stream_notify', stream_buffer)

    def samples_get(self, start=None, stop=None, units=None):
        """Get exact samples over a range.

        :param start: The starting time.
        :param stop: The ending time.
        :param units: The units for start and stop.
            'seconds' or None is in floating point seconds relative to the view.
            'samples' is in stream buffer sample indicies.
        """
        args = {'start': start, 'stop': stop, 'units': units}
        return self._post_block('samples_get', args)

    def statistics_get(self, start=None, stop=None, units=None, callback=None):
        """Get statistics over a range.

        :param start: The starting time.
        :param stop: The ending time.
        :param units: The units for start and stop.
            'seconds' or None is in floating point seconds relative to the view.
            'samples' is in stream buffer sample indices.
        :param callback: The optional callable.  When provided, this method will
            not block and the callable will be called with the statistics
            data structure from the view thread.
        :return: The statistics data structure or None if callback is provided.

            {
              "time": {
                "range": [4.2224105, 4.7224105],  # in buffer (not necessarily view) coordinates
                "delta": 0.5,
                "units": "s"
              },
              "signals": {
                "current": {
                  "statistics": {
                    "μ": 1.1410409683776379e-07,
                    "σ": 3.153094851882088e-08,
                    "min": 2.4002097531727884e-10,
                    "max": 2.77493541034346e-07,
                    "p2p": 2.772535200590287e-07
                  },
                  "units": "A",
                  "integral_units": "C"
                },
                "voltage": {
                  "statistics": {
                    "μ": 3.2984893321990967,
                    "σ": 0.0010323672322556376,
                    "min": 3.293551445007324,
                    "max": 3.3026282787323,
                    "p2p": 0.009076833724975586
                  },
                  "units": "V",
                  "integral_units": null
                },
                "power": {
                  "statistics": {
                    "μ": 3.763720144434046e-07,
                    "σ": 1.0400773930996365e-07,
                    "min": 7.916107769290193e-10,
                    "max": 9.155134534921672e-07,
                    "p2p": 9.147218427152382e-07
                  },
                  "units": "W",
                  "integral_units": "J"
                }
              }
            }

        Note: this same format is used by the :meth:`Driver.statistics_callback`.
        """
        args = {'start': start, 'stop': stop, 'units': units}
        if callback is None:
            return self._post_block('statistics_get', args)
        else:
            self._post('statistics_get', args=args, cbk=callback)
            return None

    def ping(self, *args, **kwargs):
        """Ping the thread.

        :param args: The positional arguments.
        :param kwargs: The keyword arguments.
        :return: (args, kwargs) after passing through the thread.
        """
        return self._post_block('ping', (args, kwargs))
