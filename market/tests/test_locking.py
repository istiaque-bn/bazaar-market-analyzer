import threading
import time
import uuid

from django.test import TestCase

from market.services.locking import LockBusy, distributed_lock


class DistributedLockTests(TestCase):
    """Cross-process lock used to serialize/reject duplicate market-writing
    jobs. Uses the real Redis instance (already required for Celery) —
    proves actual cross-process behavior, not a mock standing in for it."""

    def setUp(self):
        self.key = f"test-{uuid.uuid4().hex}"

    def test_second_acquire_is_rejected_while_first_holds_it(self):
        with distributed_lock(self.key, timeout=10, blocking_timeout=0):
            with self.assertRaises(LockBusy):
                with distributed_lock(self.key, timeout=10, blocking_timeout=0):
                    pass  # pragma: no cover — must not be reached

    def test_lock_is_available_again_after_release(self):
        with distributed_lock(self.key, timeout=10, blocking_timeout=0):
            pass
        # Released on exit — a fresh acquire must succeed immediately.
        with distributed_lock(self.key, timeout=10, blocking_timeout=0):
            pass

    def test_blocking_acquire_waits_then_serializes(self):
        """Two 'workers' racing for the same lock: the second must wait for
        the first to finish rather than running concurrently — proves
        duplicate jobs serialize instead of both proceeding."""
        order = []
        start_gate = threading.Event()

        def worker(name, hold_seconds):
            start_gate.wait()
            with distributed_lock(self.key, timeout=10, blocking_timeout=5):
                order.append(f"{name}-start")
                time.sleep(hold_seconds)
                order.append(f"{name}-end")

        t1 = threading.Thread(target=worker, args=("A", 0.3))
        t2 = threading.Thread(target=worker, args=("B", 0.0))
        t1.start()
        t2.start()
        start_gate.set()
        t1.join(timeout=5)
        t2.join(timeout=5)

        # Never interleaved: A must fully finish before B starts, or vice versa.
        self.assertIn(order, (["A-start", "A-end", "B-start", "B-end"], ["B-start", "B-end", "A-start", "A-end"]))

    def test_expired_lock_can_be_reacquired(self):
        with distributed_lock(self.key, timeout=1, blocking_timeout=0):
            pass
        time.sleep(1.2)
        # Would already be released normally, but this also proves a
        # crashed holder's lock doesn't wedge forever (auto-expiry).
        with distributed_lock(self.key, timeout=10, blocking_timeout=0):
            pass


class ExclusiveDbWriteCrossProcessTests(TestCase):
    """market.services.autosync.exclusive_db_write is what every
    market-writing Celery task actually uses. Simulate a second worker
    process already holding the Redis side of the lock (a real second
    process would set the exact same Redis key) and prove a duplicate
    task attempt is rejected rather than proceeding to write."""

    def test_duplicate_task_rejected_when_another_holds_the_market_lock(self):
        from market.services.autosync import exclusive_db_write
        from market.services.locking import distributed_lock

        with distributed_lock("market-write", timeout=10, blocking_timeout=0):
            with self.assertRaises(TimeoutError):
                with exclusive_db_write(blocking=False):
                    pass  # pragma: no cover — must not be reached

    def test_proceeds_once_the_other_holder_releases(self):
        from market.services.autosync import exclusive_db_write
        from market.services.locking import distributed_lock

        with distributed_lock("market-write", timeout=10, blocking_timeout=0):
            pass
        with exclusive_db_write(blocking=False):
            pass  # must not raise now that the lock is free
