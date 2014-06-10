import yaml
import argparse
import datetime
import setproctitle
import gevent
import signal
import time

from gevent import spawn, sleep
from gevent.monkey import patch_all
from gevent.event import Event
patch_all()
import logging
from pprint import pformat

from .stratum_server import StratumManager
from .monitor import MonitorWSGI, StatManager
from .utils import import_helper


def main():
    parser = argparse.ArgumentParser(description='Run powerpool!')
    parser.add_argument('config', type=argparse.FileType('r'),
                        help='yaml configuration file to run with')
    args = parser.parse_args()

    # override those defaults with a loaded yaml config
    raw_config = yaml.load(args.config) or {}

    # check that config has a valid address
    server = PowerPool(raw_config, **raw_config['powerpool'])
    server.run()


class PowerPool(object):
    def __init__(self, raw_config, procname="powerpool", term_timeout=3, loggers=None):
        if not loggers:
            loggers = [{'type': 'StreamHandler', 'level': 'DEBUG'}]

        self.log_handlers = []

        # setup all our log handlers
        for log_cfg in loggers:
            handler = getattr(logging, log_cfg['type'])()
            log_level = getattr(logging, log_cfg['level'].upper())
            handler.setLevel(log_level)
            fmt = log_cfg.get('format', '%(asctime)s [%(name)s] [%(levelname)s] %(message)s')
            formatter = logging.Formatter(fmt)
            handler.setFormatter(formatter)
            self.log_handlers.append((log_cfg.get('listen'), handler))

        self.logger = self.register_logger('manager')

        self.logger.info("=" * 80)
        self.logger.info("PowerPool stratum server ({}) starting up...".format(procname))
        self.logger.debug(pformat(raw_config))

        setproctitle.setproctitle(procname)
        self.term_timeout = term_timeout
        self.raw_config = raw_config

        # bookkeeping for things to request exit from at exit time
        # A list of all the greenlets that are running
        self.greenlets = []
        # A list of all the StreamServers
        self.servers = []

        # Primary systems
        self.stratum_manager = None
        # The network monitor object
        self.jobmanager = None
        # The module that reports everything to the outside
        self.reporter = None

        self.stratum_servers = []
        self.agent_servers = []
        self.monitor_server = None

        # Stats tracking for the whole server
        #####
        self.server_start = datetime.datetime.utcnow()
        # shares
        self.shares = StatManager()
        self.reject_low = StatManager()
        self.reject_dup = StatManager()
        self.reject_stale = StatManager()
        # connections
        self.stratum_connects = StatManager()
        self.stratum_disconnects = StatManager()
        self.agent_connects = StatManager()
        self.agent_disconnects = StatManager()

    def register_logger(self, name):
        logger = logging.getLogger(name)
        for keys, handler in self.log_handlers:
            if not keys or name in keys:
                logger.addHandler(handler)
                # handlers will manage level, so just propogate everything
                logger.setLevel(logging.DEBUG)

        return logger

    def run(self):
        # Start the main chain network monitor and aux chain monitors
        self.logger.info("Reporter engine starting up")
        cls = import_helper(self.raw_config['reporter']['type'])
        self.reporter = cls(self, **self.raw_config['reporter'])
        self.reporter.start()
        self.greenlets.append(self.reporter)

        # main stratum server manager, not actually a greenelt but starts
        # several servers and manages data structures
        self.stratum_manager = StratumManager(self, **self.raw_config.get('stratum', {}))
        self.servers.extend(self.stratum_manager.stratum_servers)
        self.servers.extend(self.stratum_manager.agent_servers)

        # Network monitor is in charge of job generation...
        self.logger.info("Network monitor starting up")
        cls = import_helper(self.raw_config['jobmanager']['type'])
        self.jobmanager = cls(self, **self.raw_config['jobmanager'])
        self.jobmanager.start()
        self.greenlets.append(self.jobmanager)

        # a simple greenlet that rotates some of the servers stats
        self.stat_rotater = spawn(self.tick_stats)
        self.greenlets.append(self.stat_rotater)

        # the monitor server. a simple flask http server that lets you view
        # internal data structures to monitor server health
        self.monitor_server = MonitorWSGI(**self.raw_config.get('monitor', {}))
        if self.monitor_server:
            self.monitor_server.start()
            self.servers.append(self.monitor_server)

        gevent.signal(signal.SIGINT, self.exit, "SIGINT")
        gevent.signal(signal.SIGHUP, self.exit, "SIGHUP")

        self._exit_signal = Event()
        self._exit_signal.wait()

        # stop all stream servers
        for server in self.servers:
            # timeout is actually the time we wait before killing the greenlet,
            # so don't bother waiting, no cleanup is needed from our servers
            spawn(server.stop, timeout=0)

        # stop all greenlets
        for gl in self.greenlets:
            gl.kill(timeout=self.term_timeout, block=False)

        try:
            if gevent.wait(timeout=self.term_timeout):
                self.logger.info("All threads exited normally")
            else:
                from greenlet import greenlet
                import gc
                import traceback
                for ob in gc.get_objects():
                    if not isinstance(ob, greenlet):
                        continue
                    if not ob:
                        continue
                    self.logger.error(''.join(traceback.format_stack(ob.gr_frame)))
                self.logger.info("Timeout reached, shutting down forcefully")
        except KeyboardInterrupt:
            self.logger.info("Shutdown requested again by system, "
                        "exiting without cleanup")

        self.logger.info("=" * 80)

    def exit(self, signal=None):
        self.logger.info("*" * 80)
        self.logger.info("Exiting requested via {}, allowing {} seconds for cleanup."
                         .format(signal, self.term_timeout))
        self._exit_signal.set()

    def tick_stats(self):
        try:
            self.logger.info("Stat rotater starting up")
            last_tick = int(time.time())
            last_send = (int(time.time()) // 60) * 60
            while True:
                now = time.time()
                # time to rotate minutes?
                if now > (last_send + 60):
                    shares = self.shares.tock()
                    reject_low = self.reject_low.tock()
                    reject_dup = self.reject_dup.tock()
                    reject_stale = self.reject_stale.tock()
                    self.stratum_connects.tock()
                    self.stratum_disconnects.tock()
                    self.agent_connects.tock()
                    self.agent_disconnects.tock()

                    if shares or reject_dup or reject_low or reject_stale:
                        self.reporter.add_one_minute(
                            'pool', shares, now, '', reject_dup,
                            reject_low, reject_stale)
                    last_send += 60

                # time to tick?
                if now > (last_tick + 1):
                    self.shares.tick()
                    self.reject_low.tick()
                    self.reject_dup.tick()
                    self.reject_stale.tick()
                    self.stratum_connects.tick()
                    self.stratum_disconnects.tick()
                    self.agent_connects.tick()
                    self.agent_disconnects.tick()
                    last_tick += 1

                sleep(0.1)
        except gevent.GreenletExit:
            self.logger.info("Stat manager exiting...")