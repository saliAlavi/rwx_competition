"""An in-process stand-in for concurrent.futures.ProcessPoolExecutor.

sqlglot's optimizer and executor tests send work to a "spawn" process pool
sized to the host's CPU count. Inside the scorer's container every spawned
worker re-imports sqlglot, duckdb and pandas, and all of that CPU is billed.
Running the work in-process is far cheaper. Arguments and results take a
pickle round trip so a change that breaks pickling still fails the test, as
it would across a real process boundary.
"""

from __future__ import annotations

import concurrent.futures
import pickle
from concurrent.futures import Future


BROKEN = {"flag": False}  # a real pool would now be unusable for the rest of its class


class SyncProcessPool:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **_):
        self._broken = None
        if initializer is not None:
            try:
                initializer(*pickle.loads(pickle.dumps(tuple(initargs))))
            except BaseException as error:  # a real pool reports this as BrokenProcessPool
                self._broken = error

    def submit(self, fn, /, *args, **kwargs):
        future: Future = Future()
        if self._broken is not None:
            future.set_exception(
                concurrent.futures.process.BrokenProcessPool(repr(self._broken))
            )
            return future
        try:
            fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            future.set_exception(error)
            return future
        try:
            result = fn(*args, **kwargs)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            try:  # the worker pickles the exception back to the parent
                pickle.loads(pickle.dumps(error))
            except BaseException:
                BROKEN["flag"] = True
            future.set_exception(error)
            return future
        try:
            result = pickle.loads(pickle.dumps(result))
        except BaseException as error:
            # "A result has failed to un-serialize": the real pool breaks for good
            BROKEN["flag"] = True
            future.set_exception(concurrent.futures.process.BrokenProcessPool(repr(error)))
            return future
        future.set_result(result)
        return future

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        futures = [self.submit(fn, *items) for items in zip(*iterables)]
        return (future.result() for future in futures)

    def shutdown(self, wait=True, *, cancel_futures=False):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.shutdown()
        return False


def limit_threads() -> None:
    """Keep native libraries to one thread: the scorer allows 256 processes
    (threads count) and bills every CPU second, so parallel BLAS/duckdb only hurts."""
    import os

    for var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
                "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "RAYON_NUM_THREADS"):
        os.environ[var] = "1"
    try:
        import duckdb
    except Exception:
        return
    if getattr(duckdb.connect, "_br_wrapped", False):
        return
    original = duckdb.connect

    def connect(database=":memory:", read_only=False, config=None, **kwargs):
        config = dict(config or {})
        config.setdefault("threads", 1)
        # duckdb 2.0 also starts `async_threads` (256 by default) I/O threads per database:
        # alone that exhausts the scorer's 256-process limit
        config.setdefault("async_threads", 1)
        return original(database, read_only=read_only, config=config, **kwargs)

    connect._br_wrapped = True
    duckdb.connect = connect


def install() -> None:
    import concurrent.futures.process  # noqa: F401  (BrokenProcessPool lives here)

    concurrent.futures.ProcessPoolExecutor = SyncProcessPool
    concurrent.futures.process.ProcessPoolExecutor = SyncProcessPool
