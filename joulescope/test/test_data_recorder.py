# Copyright 2018 Jetperch LLC
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

"""
Test the data recorder
"""

import unittest
from joulescope.data_recorder import DataRecorder, DataReader, Statistics, stats_to_api
from joulescope.stream_buffer import StreamBuffer, usb_packet_factory, usb_packet_factory_signal
from joulescope.calibration import Calibration
import io
import tempfile
import numpy as np
import os
import shutil

PACKET_HEADER_SIZE = 8
SAMPLES_PER_PACKET = (512 - PACKET_HEADER_SIZE) // 4


class TestDataRecorder(unittest.TestCase):

    def setUp(self):
        self._tempdir = tempfile.mkdtemp()
        self._filename1 = os.path.join(self._tempdir, 'f1.joulescope')

    def tearDown(self):
        shutil.rmtree(self._tempdir)

    def test_init_with_file_handle(self):
        fh = io.BytesIO()
        d = DataRecorder(fh, 2000)
        d.close()
        self.assertGreater(len(fh.getbuffer()), 0)

    def test_init_with_filename(self):
        self.assertFalse(os.path.isfile(self._filename1))
        d = DataRecorder(self._filename1, 2000)
        self.assertTrue(os.path.isfile(self._filename1))
        d.close()

    def _create_file(self, packet_index, count=None):
        stream_buffer = StreamBuffer(2000, [10], 1000.0)
        stream_buffer.suppress_mode = 'off'
        if packet_index > 0:
            data = usb_packet_factory(0, packet_index - 1)
            stream_buffer.insert(data)
            stream_buffer.process()

        fh = io.BytesIO()
        d = DataRecorder(fh, sampling_frequency=1000)
        d.stream_notify(stream_buffer)
        data = usb_packet_factory(packet_index, count)
        stream_buffer.insert(data)
        stream_buffer.process()
        d.stream_notify(stream_buffer)
        d.close()
        fh.seek(0)

        # from joulescope import datafile
        # dfr = datafile.DataFileReader(fh)
        # dfr.pretty_print()
        # fh.seek(0)
        return fh

    def test_write_read_direct(self):
        fh = self._create_file(0, 2)
        r = DataReader().open(fh)
        r.raw_processor.suppress_mode = 'off'
        data = r.get(0, 10, 1)
        np.testing.assert_allclose(np.arange(0, 20, 2), data[:, 0, 0])

    def test_time_conversion(self):
        fh = self._create_file(0, 2)
        r = DataReader().open(fh)
        self.assertEqual([0, 200], r.sample_id_range)
        self.assertEqual(1000, r.sampling_frequency)
        self.assertEqual(1000 / 10, r.reduction_frequency)
        self.assertEqual(0.2, r.duration)
        self.assertEqual(0.0, r.sample_id_to_time(0))
        self.assertEqual(0, r.time_to_sample_id(0))
        self.assertEqual(0.2, r.sample_id_to_time(200))
        self.assertEqual(200, r.time_to_sample_id(0.2))

    def test_write_read_direct_with_offset(self):
        fh = self._create_file(0, 2)
        r = DataReader().open(fh)
        r.raw_processor.suppress_mode = 'off'
        # d = np.right_shift(r.raw(5, 10), 2)
        data = r.get(5, 10, 1)
        np.testing.assert_allclose(np.arange(10, 20, 2), data[:, 0, 0])

    def test_write_read_direct_with_sample_overscan_before(self):
        fh = self._create_file(1, 3)  # will be samples 120 to 250 (not 126 to 252)
        r = DataReader().open(fh)
        r.raw_processor.suppress_mode = 'off'
        data = r.get(0, 140, 1)
        np.testing.assert_allclose(np.arange(252, 532, 2), data[:, 0, 0])

    def test_write_read_stats_over_samples(self):
        fh = self._create_file(0, 2)
        r = DataReader().open(fh)
        r.raw_processor.suppress_mode = 'off'
        data = r.get(0, 50, 5)
        np.testing.assert_allclose(np.arange(4, 100, 10), data[:, 0, 0])

    def test_write_read_stats_over_samples_offset(self):
        fh = self._create_file(0, 2)
        r = DataReader().open(fh)
        data = r.get(5, 50, 10)
        np.testing.assert_allclose(np.arange(9, 70, 20), data[:, 0, 0])

    def test_write_read_get_reduction(self):
        fh = self._create_file(0, 2)
        r = DataReader().open(fh)
        data = r.get_reduction(0, 100)
        np.testing.assert_allclose(np.arange(9, 200, 20), data[:, 0, 0])

    def test_write_read_get_reduction_offset(self):
        fh = self._create_file(0, 2)
        r = DataReader().open(fh)
        data = r.get_reduction(30, 95)
        np.testing.assert_allclose(np.arange(69, 180, 20), data[:, 0, 0])

    def test_write_read_reduction_direct(self):
        fh = self._create_file(0, 2)
        r = DataReader().open(fh)
        data = r.get(0, 100, 10)
        np.testing.assert_allclose(np.arange(9, 200, 20), data[:, 0, 0])

    def test_write_read_reduction_indirect(self):
        fh = self._create_file(0, 2)
        r = DataReader().open(fh)
        data = r.get(0, 200, 20)
        np.testing.assert_allclose(np.arange(19, 400, 40), data[:, 0, 0])

    def _create_large_file(self, samples=None):
        """Create a large file.

        :param samples: The total number of samples which will be rounded
            to a full USB packet.
        """
        sample_rate = 2000000
        samples_total = sample_rate * 2
        packets_per_burst = 128
        bursts = int(np.ceil(samples / (SAMPLES_PER_PACKET * packets_per_burst)))
        stream_buffer = StreamBuffer(sample_rate, [100], sample_rate)

        fh = io.BytesIO()
        d = DataRecorder(fh, sampling_frequency=sample_rate)
        d.stream_notify(stream_buffer)
        for burst_index in range(bursts):
            packet_index = burst_index * packets_per_burst
            frames = usb_packet_factory_signal(packet_index, count=packets_per_burst, samples_total=samples_total)
            stream_buffer.insert(frames)
            stream_buffer.process()
            d.stream_notify(stream_buffer)
        d.close()
        fh.seek(0)

        # dfr = datafile.DataFileReader(fh)
        # dfr.pretty_print()
        # fh.seek(0)

        return fh

    def test_large_file_from_usb(self):
        sample_count = 2000000 * 2
        fh = self._create_large_file(sample_count)
        r = DataReader().open(fh)
        self.assertEqual([0, sample_count], r.sample_id_range)
        reduction = r.get_reduction()
        self.assertEqual(sample_count / 20000, len(reduction))

    def create_sinusoid_data(self, sample_rate, samples):
        x = np.arange(samples, dtype=np.float)
        x *= (1 / sample_rate)
        data = np.empty(samples * 2, dtype=np.uint16)
        data[0::2] = (2000 * np.sin(2 * np.pi * 1000 * x) + 5000).astype(np.uint16)
        data[1::2] = (2000 * np.cos(2 * np.pi * 42 * x) + 5000).astype(np.uint16)
        np.left_shift(data, 2, out=data)
        data_view = data[1::4]
        np.bitwise_or(data_view, 0x20, out=data_view)
        return data

    def create_sinusoid_file(self, sample_rate, samples):
        cal = Calibration()
        cal.current_offset[:7] = -3000
        cal.current_gain[:7] = [1e-3, 1e-4, 1e-5, 1e-6, 1e-7, 1e-8, 1e-9]
        cal.voltage_offset[:2] = -3000
        cal.voltage_gain[:2] = [1e-3, 1e-4]
        cal.data = cal.save(bytes([0] * 32))

        fh = io.BytesIO()
        d = DataRecorder(fh, sampling_frequency=sample_rate, calibration=cal)

        stream_buffer = StreamBuffer(sample_rate, [100], sample_rate)
        stream_buffer.calibration_set(cal.current_offset, cal.current_gain, cal.voltage_offset, cal.voltage_gain)
        d.stream_notify(stream_buffer)
        data = self.create_sinusoid_data(sample_rate, samples)

        chunk_size = (sample_rate // 2) * 2
        for i in range(0, 2 * samples, chunk_size):
            stream_buffer.insert_raw(data[i:(i + chunk_size)])
            stream_buffer.process()
            d.stream_notify(stream_buffer)

        d.close()
        fh.seek(0)
        return fh

    def test_statistics_get(self):
        #fh = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data_recording_01.jls')
        fh = self.create_sinusoid_file(2000000, 400000)
        r = DataReader().open(fh)

        t_start, t_stop = 0.066780, 0.069004
        k_start, k_stop = r.normalize_time_arguments(t_start, t_stop, units='seconds')
        ranges = [
            (0, 1000),         # trivial, direct
            (0, 20000),        # trivial, single reduction
            (100000, 101000),  # offset, direct
            (100000, 120000),  # offset, ex
            (99000, 120000),
            (100000, 121000),
            (99000, 121000),
            (k_start, k_stop),
        ]

        for k_start, k_stop in ranges:
            # print(f'range {k_start}:{k_stop}')
            s1 = r.statistics_get(k_start, k_stop, units='samples')
            _, _, data = r.raw(k_start, k_stop)
            i_mean = np.mean(data[:, 0])
            np.testing.assert_allclose(s1['signals']['current']['statistics']['μ'], i_mean, rtol=0.0005)
        r.close()

    def test_cache_test(self):
        sample_rate = 2000000
        sample_count = sample_rate * 2
        fh = self.create_sinusoid_file(sample_rate, sample_count)
        r = DataReader().open(fh)
        for step_size in [1111, 2000, 11111, 20000]:
            # print(f'step_size = {step_size}')
            for i in range(0, sample_count - step_size, step_size):
                r.raw_processor.reset()
                s1 = r.statistics_get(i, i + step_size, units='samples')
                _, _, data = r.raw(i, i + step_size)
                i_mean = np.mean(data[:, 0])
                np.testing.assert_allclose(s1['signals']['current']['statistics']['μ'], i_mean, rtol=0.0005)
        r.close()

    def test_single_sample(self):
        fh = self.create_sinusoid_file(2000000, 400000)
        r = DataReader().open(fh)
        s1 = r.statistics_get(20, 20, units='samples')
        i_mean = r.raw(20, 21)[2][0, 0]
        np.testing.assert_allclose(s1['signals']['current']['statistics']['μ'], i_mean, rtol=0.0005)
