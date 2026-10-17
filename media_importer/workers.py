"""Recover crashed background workers; fail the process if a critical one stays down."""
import logging
import threading
import time


class Workers:
    def __init__(self, stop, fatal):
        self.stop, self.fatal = stop, fatal
        self.threads, self.states = [], {}
        self.lock = threading.Lock()

    def snapshot(self):
        with self.lock:
            return {name: dict(state) for name, state in self.states.items()}

    def run(self, name, target, critical=True):
        failures = 0
        while not self.stop.is_set():
            started = time.monotonic()
            with self.lock:
                self.states[name] = {'state': 'running', 'restarts': failures, 'critical': critical}
            try:
                target()
                if self.stop.is_set():
                    return
                raise RuntimeError('Worker returned before shutdown')
            except Exception:
                if self.stop.is_set():
                    return
                failures = 1 if time.monotonic() - started >= 60 else failures + 1
                with self.lock:
                    self.states[name].update(state='failed' if critical and failures >= 3 else 'retrying', restarts=failures)
                # Raw exception values can contain signed URLs or credentials.
                logging.error('后台线程 %s 意外退出，重试次数 %s', name, failures)
                if critical and failures >= 3:
                    self.fatal()
                    return
                self.stop.wait(min(30, 2 ** min(failures - 1, 5)))

    def start(self, name, target, critical=True):
        thread = threading.Thread(target=self.run, args=(name, target, critical), name=name, daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread
