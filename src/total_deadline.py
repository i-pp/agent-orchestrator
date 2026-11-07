"""Wall-clock deadline helper for interruptible socket operations."""
import math
import threading
import time


class TotalDeadline:
    """Run one abort callback after a wall-clock budget, even across trickle I/O."""

    def __init__(self, seconds, abort):
        if (type(seconds) not in (int, float) or not math.isfinite(seconds)
                or seconds <= 0 or not callable(abort)):
            raise ValueError('Invalid total deadline')
        self.seconds = float(seconds)
        self.abort = abort
        self._expired = threading.Event()
        self._end = None
        self._timer = threading.Timer(self.seconds, self._expire)
        self._timer.daemon = True

    @property
    def expired(self):
        return self._expired.is_set()

    @property
    def remaining(self):
        if self._end is None:
            return self.seconds
        return max(0.0, self._end - time.monotonic())

    def _expire(self):
        self._expired.set()
        try:
            self.abort()
        except Exception:
            pass

    def start(self):
        self._end = time.monotonic() + self.seconds
        self._timer.start()

    def cancel(self):
        self._timer.cancel()


def run_with_total_deadline(seconds, abort, operation):
    """Interrupt an operation at one total deadline and normalize that timeout."""
    deadline = TotalDeadline(seconds, abort)
    deadline.start()
    try:
        try:
            result = operation()
        except BaseException:
            if deadline.expired:
                raise TimeoutError('Total deadline exceeded') from None
            raise
        if deadline.expired:
            raise TimeoutError('Total deadline exceeded')
        return result
    finally:
        deadline.cancel()
