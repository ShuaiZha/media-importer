"""Track useful activity, separately from the control thread's heartbeat."""
import threading
import time
from contextlib import contextmanager

from .model import now


class CombinedStop:
    def __init__(self, *events):
        self.events = tuple(e for e in events if e is not None)

    def is_set(self):
        return any(e.is_set() for e in self.events)

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            remaining = .1 if deadline is None else min(.1, deadline - time.monotonic())
            if remaining <= 0:
                return False
            if self.events:
                self.events[0].wait(remaining)
            else:
                time.sleep(remaining)
        return True


class Activity:
    def __init__(self, timeouts, clock=time.monotonic):
        self.timeouts, self.clock = timeouts, clock
        self.lock = threading.RLock()
        self.states = {}

    def begin(self, stage, operation='work'):
        with self.lock:
            self.states[stage] = {'state': 'active', 'operation': operation,
                                  'activity_at': now(), 'tick': self.clock(),
                                  'timeout_seconds': self.timeouts[stage]}

    def touch(self, stage):
        with self.lock:
            item = self.states.get(stage)
            if item and item['state'] == 'active':
                item.update(activity_at=now(), tick=self.clock())

    def idle(self, stage):
        with self.lock:
            item = self.states.get(stage)
            if item and item['state'] != 'stalled':
                item.update(state='waiting', operation='waiting')

    @contextmanager
    def waiting(self, stage):
        with self.lock:
            active = self.states.get(stage, {}).get('state') == 'active'
            if active:
                self.states[stage]['state'] = 'waiting'
        try:
            yield
        finally:
            with self.lock:
                if active and self.states.get(stage, {}).get('state') == 'waiting':
                    self.states[stage].update(state='active', activity_at=now(), tick=self.clock())

    def expired(self):
        with self.lock:
            result = []
            for stage, item in self.states.items():
                if item['state'] == 'active' and self.clock() - item['tick'] > item['timeout_seconds']:
                    item['state'] = 'stalled'
                    result.append(stage)
            return result

    def snapshot(self):
        with self.lock:
            return {stage: {k: v for k, v in item.items() if k != 'tick'} for stage, item in self.states.items()}
