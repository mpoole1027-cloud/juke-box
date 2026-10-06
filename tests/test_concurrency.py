"""Request threads and the worker thread share the tracking globals.

Routes go through the public advance_to_next_pending(), which takes _lock;
the worker calls the private form because it already holds it. _lock is not
reentrant, so getting this wrong deadlocks the whole app.
"""
import threading
import time

from conftest import FALLBACK_ID


def test_routes_and_worker_do_not_deadlock(qm, db):
    db.set_setting('fallback_playlist_id', FALLBACK_ID)
    qm.fake.playing = 'x' * 22
    qm.fake.is_playing = True

    stop = threading.Event()
    errors = []

    def as_worker():
        try:
            while not stop.is_set():
                with qm._lock:
                    qm._advance_to_next_pending()
                time.sleep(0.001)
        except Exception as e:      # pragma: no cover
            errors.append(e)

    def as_route():
        try:
            while not stop.is_set():
                qm.advance_to_next_pending()
                time.sleep(0.001)
        except Exception as e:      # pragma: no cover
            errors.append(e)

    threads = ([threading.Thread(target=as_worker, daemon=True) for _ in range(2)] +
               [threading.Thread(target=as_route, daemon=True) for _ in range(4)])
    for t in threads:
        t.start()
    time.sleep(1.5)
    stop.set()
    for t in threads:
        t.join(timeout=5)

    assert not errors, errors
    stuck = [t for t in threads if t.is_alive()]
    assert not stuck, f"deadlock: {len(stuck)} threads never exited"
    assert qm.fake.calls, "threads should have made progress"


def test_public_entry_point_is_not_reentrant_safe_by_accident(qm):
    """The worker must never call the public form while holding _lock."""
    import inspect
    src = inspect.getsource(qm.background_worker)
    assert 'advance_to_next_pending()' in src
    assert 'qm.advance_to_next_pending' not in src
    # The worker calls the private form only.
    for line in src.splitlines():
        if 'advance_to_next_pending' in line:
            assert '_advance_to_next_pending' in line, line
