import logging
from time import sleep, time
from itertools import izip
from threading import Thread, Event
from collections import deque, Iterable
from Queue import Queue, Empty
import sys

import numpy as np

from basil.HL import sitcp_fifo
from pybar.utils.utils import get_float_time
from pybar.daq.readout_utils import is_fe_word, is_data_record, is_data_header, logical_or, logical_and, data_array_from_data_iterable, convert_data_iterable, convert_data_array


data_iterable = ("data", "timestamp_start", "timestamp_stop", "error")


class RxSyncError(Exception):
    pass


class EightbTenbError(Exception):
    pass


class FifoError(Exception):
    pass


class NoDataTimeout(Exception):
    pass


class StopTimeout(Exception):
    pass


class FifoReadout(object):
    def __init__(self, dut):
        self.dut = dut
        self.callback = None
        self.errback = None
        self.readout_thread = None
        self.worker_thread = None
        self.watchdog_thread = None
        self.fifos = []
        self.fill_buffer = False
        self.filter_func = None
        self.converter_func = None
        self.fifo_select = None
        self.enabled_fe_channels = None
        self.enabled_m26_channels = None
        self.readout_interval = 0.05
        self._moving_average_time_period = 10.0
        self._n_empty_reads = 3  # number of empty reads before stopping FIFO readout
        self._data_deque = None
        self._data_buffer = None
        self._words_per_read = deque(maxlen=int(self._moving_average_time_period / self.readout_interval))
        self._result = Queue(maxsize=1)
        self._calculate = Event()
        self.stop_readout = Event()
        self.force_stop = None
        self.timestamp = None
        self._is_running = False

    @property
    def is_running(self):
        return self._is_running

    @property
    def is_alive(self):
        if self.worker_thread:
            return self.worker_thread.is_alive()
        else:
            False

    def data_words_per_second(self):
        if self._result.full():
            self._result.get()
        self._calculate.set()
        try:
            result = self._result.get(timeout=2 * self.readout_interval)
        except Empty:
            self._calculate.clear()
            return None
        return result / float(self._moving_average_time_period)

    def start(self, fifos, callback=None, errback=None, reset_rx=False, reset_fifo=False, fill_buffer=False, no_data_timeout=None, filter_func=None, converter_func=None, fifo_select=None, enabled_fe_channels=None, enabled_m26_channels=None):
        if self._is_running:
            raise RuntimeError('FIFO readout threads already started: use stop()')

        if not isinstance(fifos, Iterable):
            fifos = [fifos]
        if len(set(fifos)) != len(fifos):
            raise ValueError("The following FIFOs are occurring multiple times: %s" % set([fifo for fifo in fifos if fifos.count(fifo) > 1]))
        if isinstance(fifo_select, Iterable) and set(fifo_select) - set(fifos):
            raise ValueError("The following FIFOs have filters/converters set but are not read out: %s" % (set(fifo_select) - set(fifos)))
        if isinstance(filter_func, Iterable) or isinstance(converter_func, Iterable) or isinstance(fifo_select, Iterable):
            if len(filter_func) != len(converter_func):
                raise ValueError("Length of filters and converters not equal.")
            if len(filter_func) != len(converter_func):
                raise ValueError("Length of filters and converters not equal.")
            if len(filter_func) != len(fifo_select):
                raise ValueError("Length of filters/converters and selected FIFOs not equal.")
        else:
            filter_func = [filter_func]
            converter_func = [converter_func]
            fifo_select = [fifo_select]
            
        if enabled_fe_channels is None:
            self.enabled_fe_channels = [rx.name for rx in self.dut.get_modules('fei4_rx')]
        else:
            self.enabled_fe_channels = enabled_fe_channels
        if enabled_m26_channels is None:
            self.enabled_m26_channels = [rx.name for rx in self.dut.get_modules('m26_rx')]
        else:
            self.enabled_m26_channels = enabled_m26_channels
        self.fifos = fifos
        self.callback = callback
        self.errback = errback
        self.fill_buffer = fill_buffer
        self.filter_func = filter_func        
        self.converter_func = converter_func
        self.fifo_select = fifo_select

        self._data_deque = {fifo: deque() for fifo in self.fifos}
        self.timestamp = {fifo: None for fifo in self.fifos}
        self.force_stop = {fifo: Event() for fifo in self.fifos}
        self._data_buffer = [deque() for _ in self.filter_func]
        self._is_running = True
        if reset_rx:
            self.reset_rx(fe_channels=self.enabled_fe_channels, m26_channels=self.enabled_m26_channels)
        for fifo in self.fifos:
            if reset_fifo:
                self.reset_fifo([fifo])
            self.update_timestamp(fifo)
            fifo_size = self.get_fifo_size(fifo)
            if fifo_size != 0:
                logging.warning('%s contains data: FIFO_SIZE = %i', fifo, fifo_size)
        self._words_per_read.clear()
        self.stop_readout.clear()
        for event in self.force_stop.values():
            event.clear()
        logging.info('Starting FIFO readout...')
        if self.errback:
            self.watchdog_thread = Thread(target=self.watchdog, name='WatchdogThread')
            self.watchdog_thread.daemon = True
            self.watchdog_thread.start()
        self.readout_thread = []
        self.worker_thread = []
        for fifo in self.fifos:
            readout_thread = Thread(target=self.readout, name='%s ReadoutThread' % fifo, kwargs={'fifo': fifo, 'no_data_timeout': no_data_timeout})
            worker_thread = Thread(target=self.worker, name='%s WorkerThread' % fifo, kwargs={'fifo': fifo})
            readout_thread.daemon = True
            worker_thread.daemon = True
            self.readout_thread.append(readout_thread)
            self.worker_thread.append(worker_thread)
        for worker_thread in self.worker_thread:
            worker_thread.start()
        for readout_thread in self.readout_thread:
            self.update_timestamp(fifo)
            readout_thread.start()
        # enabling RX channels
        for fifo in self.fifos:
            self.update_timestamp(fifo)
        for fei4_rx_name in self.enabled_fe_channels:
            self.dut[fei4_rx_name].ENABLE_RX = 1
        for m26_rx_name in self.enabled_m26_channels:
            self.dut[m26_rx_name].EN = 1

    def stop(self, timeout=10.0):
        if not self._is_running:
            raise RuntimeError('FIFO readout threads not running: use start()')
        # disabling Mimosa26 RX channels
        for m26_rx_name in self.enabled_m26_channels:
            self.dut[m26_rx_name].EN = 0
        self.stop_readout.set()
        def wait_for_thread_timeout(thread, fifo, timeout):
            try:
                thread.join(timeout=timeout)
                if thread.is_alive():
                    raise StopTimeout('Stopping %s readout thread timed out after %0.1fs' % (fifo, timeout))
            except StopTimeout, e:
                self.force_stop[fifo].set()
                if self.errback:
                    self.errback(sys.exc_info())
                else:
                    logging.error(e)

        join_threads = []
        for i, fifo in enumerate(self.fifos):
            join_thread = Thread(target=wait_for_thread_timeout, kwargs={'thread': self.readout_thread[i], 'fifo': fifo, 'timeout': timeout})
            join_thread.daemon = True
            join_thread.start()
            join_threads.append(join_thread)
        for join_thread in join_threads:
            if join_thread.is_alive():
                join_thread.join()
        for readout_thread in self.readout_thread:
            if readout_thread.is_alive():
                readout_thread.join()
        for worker_thread in self.worker_thread:
            worker_thread.join()
        if self.errback:
            self.watchdog_thread.join()
        # disabling FEI4 RX channels
        for fei4_rx_name in self.enabled_fe_channels:
            self.dut[fei4_rx_name].ENABLE_RX = 0
        self.callback = None
        self.errback = None
        self._is_running = False
        logging.info('Stopped FIFO readout')

    def print_readout_status(self, active_module):
        for fifo in self.fifos:
            logging.info('Data queue size: %d', len(self._data_deque[fifo]))
            logging.info('FIFO size: %d', self.get_fifo_size(fifo))
        # FEI4
        enable_status = self.get_rx_enable_status()
        sync_status = self.get_rx_sync_status()
        discard_count = self.get_rx_fifo_discard_count()
        error_count = self.get_rx_8b10b_error_count()
        fei4_rx_names = [rx.name for rx in self.dut.get_modules('fei4_rx')]
        active_modules = self.get_active_modules(fei4_rx_names, active_module)
        if fei4_rx_names:
            logging.info('FEI4 RX channel:                  %s', " | ".join([name.rjust(3) for name in fei4_rx_names]))
            logging.info('FEI4 RX enabled:                  %s', " | ".join(["YES".rjust(max(3, len(fei4_rx_names[index]))) if status is True else "NO".rjust(max(3, len(fei4_rx_names[index]))) for index, status in enumerate(active_modules)]))
            logging.info('FEI4 RX sync:                     %s', " | ".join(["YES".rjust(max(3, len(fei4_rx_names[index]))) if status is True else "NO".rjust(max(3, len(fei4_rx_names[index]))) for index, status in enumerate(sync_status)]))
            logging.info('FEI4 RX FIFO discard counter:     %s', " | ".join([repr(count).rjust(max(3, len(fei4_rx_names[index]))) for index, count in enumerate(discard_count)]))
            logging.info('FEI4 RX FIFO 8b10b error counter: %s', " | ".join([repr(count).rjust(max(3, len(fei4_rx_names[index]))) for index, count in enumerate(error_count)]))
        if not any(sync_status) or any(discard_count) or any(error_count):
            logging.warning('FEI4 RX errors detected')
        # Mimosa26
        m26_enable_status = self.get_m26_rx_enable_status()
        m26_discard_count = self.get_m26_rx_fifo_discard_count()
        m26_rx_names = [rx.name for rx in self.dut.get_modules('m26_rx')]
        if m26_rx_names:
            logging.info('Mimosa26 RX channel:              %s', " | ".join([name.rjust(3) for name in m26_rx_names]))
            logging.info('Mimosa26 RX enabled:              %s', " | ".join(["YES".rjust(max(3, len(m26_rx_names[index]))) if status is True else "NO".rjust(max(3, len(m26_rx_names[index]))) for index, status in enumerate(m26_enable_status)]))
            logging.info('Mimosa26 RX FIFO discard counter: %s', " | ".join([repr(count).rjust(max(3, len(m26_rx_names[index]))) for index, count in enumerate(m26_discard_count)]))
        if any(m26_discard_count):
            logging.warning('Mimosa26 RX errors detected')

    def readout(self, fifo, no_data_timeout=None):
        '''Readout thread continuously reading FIFO.

        Readout thread, which uses read_raw_data_from_fifo() and appends data to self._data_deque (collection.deque).
        '''
        logging.info('Starting readout thread for %s', fifo)
        curr_time = get_float_time()
        time_wait = 0.0
        empty_reads = 0
        while not self.force_stop[fifo].wait(time_wait if time_wait >= 0.0 else 0.0):
            try:
                time_read = time()
                if no_data_timeout and curr_time + no_data_timeout < get_float_time():
                    raise NoDataTimeout('Received no data for %0.1f second(s) from %s' % (no_data_timeout, fifo))
                raw_data = self.read_raw_data_from_fifo(fifo)
            except Exception:
                no_data_timeout = None  # raise exception only once
                if self.errback:
                    self.errback(sys.exc_info())
                else:
                    raise
                if self.stop_readout.is_set():
                    break
            else:
                n_data_words = raw_data.shape[0]
                self._words_per_read.append(n_data_words)
                if n_data_words > 0:
                    empty_reads = 0
                    last_time, curr_time = self.update_timestamp(fifo)
                    status = 0
                    self._data_deque[fifo].append((raw_data, last_time, curr_time, status))
                elif self.stop_readout.is_set():
                    if empty_reads == self._n_empty_reads:
                        break
                    else:
                        empty_reads += 1
            finally:
                time_wait = self.readout_interval - (time() - time_read)
            if self._calculate.is_set():
                self._calculate.clear()
                self._result.put(sum(self._words_per_read))
        self._data_deque[fifo].append(None)  # last item, None will stop worker
        logging.info('Stopping readout thread for %s', fifo)

    def worker(self, fifo):
        '''Worker thread continuously calling callback function when data is available.
        '''
        logging.info('Starting worker thread for %s', fifo)
        while True:
            try:
                data_tuple = self._data_deque[fifo].popleft()
            except IndexError:
                self.stop_readout.wait(self.readout_interval)  # sleep a little bit, reducing CPU usage
            else:
                if data_tuple is None:  # if None then exit
                    break
                else:
                    converted_data_tuple_list = []
                    for i, (filter_func, converter_func, fifo_select) in enumerate(izip(self.filter_func, self.converter_func, self.fifo_select)):
                        if fifo_select is None or fifo_select == fifo:
                            # filter and do the conversion
                            converted_data_tuple = convert_data_iterable((data_tuple,), filter_func=filter_func, converter_func=converter_func)[0]
                            converted_data_tuple_list.append(converted_data_tuple)
                            if self.fill_buffer:
                                self._data_buffer[i].append(converted_data_tuple)
                        else:
                            converted_data_tuple_list.append(None)
                    if self.callback:
                        try:
                            self.callback(converted_data_tuple_list)
                        except Exception:
                            self.errback(sys.exc_info())
        logging.info('Stopping worker thread for %s', fifo)

    def watchdog(self):
        logging.debug('Starting %s', self.watchdog_thread.name)
        while True:
            try:
                if not all(self.get_rx_sync_status(channels=self.enabled_fe_channels)):
                    raise RxSyncError('FEI4 RX sync error')
                if any(self.get_rx_8b10b_error_count(channels=self.enabled_fe_channels)):
                    raise EightbTenbError('FEI4 RX 8b10b error(s) detected')
                if any(self.get_rx_fifo_discard_count(channels=self.enabled_fe_channels)):
                    raise FifoError('FEI4 RX FIFO discard error(s) detected')
                if any(self.get_m26_rx_fifo_discard_count(channels=self.enabled_m26_channels)):
                    raise FifoError('M26 RX FIFO discard error(s) detected')
            except Exception:
                self.errback(sys.exc_info())
            if self.stop_readout.wait(self.readout_interval * 10):
                break
        logging.debug('Stopping %s', self.watchdog_thread.name)

    def get_data_from_buffer(self, filter_func=None, converter_func=None):
        '''Reads local data buffer and returns data and meta data list.

        Returns
        -------
        data : list
            List of data and meta data dicts.
        '''
        if self._is_running:
            raise RuntimeError('Readout thread running')
        if not self.fill_buffer:
            logging.warning('Data buffer is not activated')
        return [convert_data_iterable(data_iterable, filter_func=filter_func, converter_func=converter_func) for data_iterable in self._data_buffer]

    def get_raw_data_from_buffer(self, filter_func=None, converter_func=None):
        '''Reads local data buffer and returns raw data array.

        Returns
        -------
        data : np.array
            An array containing data words from the local data buffer.
        '''
        if self._is_running:
            raise RuntimeError('Readout thread running')
        if not self.fill_buffer:
            logging.warning('Data buffer is not activated')
        return [convert_data_array(data_array_from_data_iterable(data_iterable), filter_func=filter_func, converter_func=converter_func) for data_iterable in self._data_buffer]

    def read_raw_data_from_fifo(self, fifo, filter_func=None, converter_func=None):
        '''Reads FIFO data and returns raw data array.

        Returns
        -------
        data : np.array
            An array containing FIFO data words.
        '''
        return convert_data_array(self.dut[fifo].get_data(), filter_func=filter_func, converter_func=converter_func)

    def update_timestamp(self, fifo):
        curr_time = get_float_time()
        last_time = self.timestamp[fifo]
        if last_time is None:
            last_time = curr_time
        self.timestamp[fifo] = curr_time
        return last_time, curr_time

    def get_fifo_size(self, fifo):
        return self.dut[fifo]['FIFO_SIZE']

    def reset_rx(self, fe_channels=None, m26_channels=None):
        logging.info('Resetting RX')
        if fe_channels is None:
            fe_channels = [rx.name for rx in self.dut.get_modules('fei4_rx')]
        if m26_channels is None:
            m26_channels = [rx.name for rx in self.dut.get_modules('m26_rx')]
        for fei4_rx_name in fe_channels:
            self.dut[fei4_rx_name].RESET
        for m26_rx_name in m26_channels:
            self.dut[m26_rx_name].RESET

    def reset_fifo(self, fifos):
        if not isinstance(fifos, Iterable):
            fifos = [fifos]
        for fifo in fifos:
            fifo_size = self.dut[fifo]['FIFO_SIZE']
            logging.info('Resetting %s: size = %i', fifo, fifo_size)
            self.dut[fifo]['RESET']
            # sleep for a while, if it is a hardware FIFO
            if not isinstance(self.dut[fifo], (sitcp_fifo.sitcp_fifo,)):
                sleep(0.2)
            fifo_size = self.dut[fifo]['FIFO_SIZE']
            if fifo_size != 0:
                logging.warning('%s not empty after reset: size = %i', fifo, fifo_size)

    def get_rx_enable_status(self, channels=None):
        if channels:
            return map(lambda channel: True if self.dut[channel].ENABLE_RX else False, channels)
        else:
            return map(lambda channel: True if channel.ENABLE_RX else False, self.dut.get_modules('fei4_rx'))

    def get_rx_sync_status(self, channels=None):
        if channels is None:
            return map(lambda channel: True if channel.READY else False, self.dut.get_modules('fei4_rx'))
        else:
            return map(lambda channel: True if self.dut[channel].READY else False, channels)

    def get_rx_8b10b_error_count(self, channels=None):
        if channels is None:
            return map(lambda channel: channel.DECODER_ERROR_COUNTER, self.dut.get_modules('fei4_rx'))
        else:
            return map(lambda channel: self.dut[channel].DECODER_ERROR_COUNTER, channels)

    def get_rx_fifo_discard_count(self, channels=None):
        if channels is None:
            return map(lambda channel: channel.LOST_DATA_COUNTER, self.dut.get_modules('fei4_rx'))
        else:
            return map(lambda channel: self.dut[channel].LOST_DATA_COUNTER, channels)

    def get_m26_rx_enable_status(self, channels=None):
        if channels is None:
            return map(lambda channel: True if channel.EN else False, self.dut.get_modules('m26_rx'))
        else:
            return map(lambda channel: True if self.dut[channel].EN else False, channels)

    def get_m26_rx_fifo_discard_count(self, channels=None):
        if channels is None:
            return map(lambda channel: channel.LOST_COUNT, self.dut.get_modules('m26_rx'))
        else:
            return map(lambda channel: self.dut[channel].LOST_COUNT, channels)

    def get_active_modules(self, fei4_rx_names, active_module):
        active_modules = []
        for name in fei4_rx_names:
            if name in active_module.keys():
                pass
            else:
                active_module[str(name)] = "False"
            active_modules.append(active_module[str(name)])
        return active_modules
