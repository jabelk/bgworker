# -*- mode: python; python-indent: 4 -*-
import multiprocessing
import os
import random
import select
import socket
import threading
import time

import ncs
from ncs.experimental import Subscriber

class Process(threading.Thread):
    """Supervisor for reacting to various events
    """
    def __init__(self, app, bg_fun, bg_fun_args=None, config_path=None):
        super(Process, self).__init__()
        self.app = app
        self.bg_fun = bg_fun
        self.config_path = config_path

        self.log = app.log
        self.name = "{}.{}".format(self.app.__class__.__module__,
                                    self.app.__class__.__name__)

        self.log.info("{} supervisor starting".format(self.name))
        self.q = multiprocessing.Queue()

        # start the config subscriber thread
        if self.config_path is not None:
            self.config_subscriber = ncs.experimental.Subscriber(app=self.app, log=self.log)
            subscriber_iter = ConfigSubscriber(self.q, self.config_path)
            subscriber_iter.register(self.config_subscriber)
            self.config_subscriber.start()

        # start the HA event listener thread
        self.ha_event_listener = HaEventListener(app=self.app, q=self.q)
        self.ha_event_listener.start()

        self.worker = None

        # read initial configuration
        with ncs.maapi.single_read_trans('{}_supervisor'.format(self.name), 'system', db=ncs.OPERATIONAL) as t_read:
            if config_path is not None:
                enabled = t_read.get_elem(self.config_path)
                self.config_enabled = bool(enabled)
            else:
                # if there is no config_path we assume the process is always enabled
                self.config_enabled = True

            # check if HA is enabled
            if t_read.exists("/tfnm:ncs-state/tfnm:ha"):
                self.ha_enabled = True
            else:
                self.ha_enabled = False

            # determine HA state if HA is enabled
            if self.ha_enabled:
                ha_mode = str(ncs.maagic.get_node(t_read, '/tfnm:ncs-state/tfnm:ha/tfnm:mode'))
                self.ha_master = (mode == 'master')


    def run(self):
        self.app.add_running_thread(self.name + ' (Supervisor)')

        self.log.info("Hello from supervisor")
        while True:
            should_run = self.config_enabled and (not self.ha_enabled or self.ha_master)

            if should_run and (self.worker is None or not self.worker.is_alive()):
                self.log.info("Background worker process should run but is not running, starting")
                if self.worker is not None:
                    self.worker_stop()
                self.worker_start()
            if self.worker is not None and self.worker.is_alive() and not should_run:
                self.log.info("Background worker process is running but should not run, stopping")
                self.worker_stop()

            import Queue
            try:
                item = self.q.get(timeout=1)
            except Queue.Empty:
                continue

            k, v = item
            self.log.info("Got an event! k: {} v: {}".format(k, v))
            if k == 'exit':
                return
            elif k == 'enabled':
                self.config_enabled = v


    def stop(self):
        """stop is called when the supervisor thread should stop and is part of
        the standard Python interface for threading.Thread
        """
        # stop CDB subscriber
        if self.config_path is not None:
            self.config_subscriber.stop()

        # stop the HA event listener
        self.ha_event_listener.stop()

        # stop the background worker process
        self.worker_stop()

        # stop us, the supervisor
        self.q.put(('exit',))
        self.join()
        self.app.del_running_thread(self.name + ' (Supervisor)')


    def worker_start(self):
        """Starts the background worker process
        """
        self.log.info("{}: starting the background worker process".format(self.name))
        # Instead of using the usual worker thread, we use a separate process here.
        # This allows us to terminate the process on package reload / NSO shutdown.
        self.worker = multiprocessing.Process(target=self.bg_fun)
        self.worker.start()


    def worker_stop(self):
        """Stops the background worker process
        """
        self.log.info("{}: stopping the background worker process".format(self.name))
        self.worker.terminate()
        self.worker.join(timeout=1)
        if self.worker.is_alive():
            self.log.error("{}: worker not terminated on time, alive: {}  process: {}".format(self, self.worker.is_alive(), self.worker))



class ConfigSubscriber(object):
    """CDB subscriber for background worker process

    It is assumed that there is an 'enabled' leaf that controls whether a
    background worker process should be enabled or disabled. Given the path to
    that leaf, this subscriber can monitor it and send any changes to the
    supervisor which in turn starts or stops the background worker process.

    The enabled leaf has to be a boolean where true means the background worker
    process is enabled and should run.
    """
    def __init__(self, q, config_path):
        self.q = q
        self.config_path = config_path

    def register(self, subscriber):
        subscriber.register(self.config_path, priority=101, iter_obj=self)

    def pre_iterate(self):
        return {'enabled': False}

    def iterate(self, keypath, operation_unused, oldval_unused, newval, state):
        state['enabled'] = newval
        return ncs.ITER_RECURSE

    def should_post_iterate(self, state):
        return True

    def post_iterate(self, state):
        self.q.put(("enabled", bool(state['enabled'])))


class HaEventListener(threading.Thread):
    def __init__(self, app, q):
        super(HaEventListener, self).__init__()
        self.app = app
        self.log = app.log
        self.q = q
        self.log.info('{} supervisor: init'.format(self))
        self.exit_flag = WaitableEvent()

    def run(self):
        self.app.add_running_thread(self.__class__.__name__ + ' (HA event listener)')

        self.log.info('run() HA event listener')
        from _ncs import events
        mask = events.NOTIF_HA_INFO
        event_socket = socket.socket()
        events.notifications_connect(event_socket, mask, ip='127.0.0.1', port=ncs.NCS_PORT)
        while True:
            rl, _, _ = select.select([self.exit_flag, event_socket], [], [])
            if self.exit_flag in rl:
                return

            notification = events.read_notification(event_socket)
            # Can this fail? Could we get a KeyError here? Afraid to catch it
            # because I don't know what it could mean.
            ha_notif_type = notification['hnot']['type']

            if ha_notif_type == events.HA_INFO_IS_MASTER:
                self.q.put(('ha-mode', 'master'))
            elif ha_notif_type == events.HA_INFO_IS_NONE:
                self.q.put(('ha-mode', 'none'))

        event_socket.close()

    def stop(self):
        self.exit_flag.set()
        self.join()
        self.app.del_running_thread(self.__class__.__name__ + ' (HA event listener)')


class WaitableEvent:
    """Provides an abstract object that can be used to resume select loops with
    indefinite waits from another thread or process. This mimics the standard
    threading.Event interface."""
    def __init__(self):
        self._read_fd, self._write_fd = os.pipe()

    def wait(self, timeout=None):
        rfds, wfds, efds = select.select([self._read_fd], [], [], timeout)
        return self._read_fd in rfds

    def is_set(self):
        return self.wait(0)

    def isSet(self):
        return self.wait(0)

    def clear(self):
        if self.isSet():
            os.read(self._read_fd, 1)

    def set(self):
        if not self.isSet():
            os.write(self._write_fd, '1')

    def fileno(self):
        """Return the FD number of the read side of the pipe, allows this object to
        be used with select.select()."""
        return self._read_fd

    def __del__(self):
        os.close(self._read_fd)
        os.close(self._write_fd)