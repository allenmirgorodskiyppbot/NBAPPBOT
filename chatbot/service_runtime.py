"""Restart failed background services without depending on the live database.

Individual workers handle expected source failures themselves. This outer guard
is for unexpected task exits, including an error while recording another error.
"""
from __future__ import annotations

from asyncio import CancelledError, sleep
from collections.abc import Awaitable, Callable, MutableMapping
import datetime as dt
import logging
import math
from time import monotonic


log = logging.getLogger('knicks.services')


def _now():
    return dt.datetime.now(dt.timezone.utc)


async def supervise_service(
    name: str,
    factory: Callable[[], Awaitable[None]],
    registry: MutableMapping[str, dict],
    *,
    initial_delay: float = 1.0,
    max_delay: float = 60.0,
    stable_seconds: float = 300.0,
):
    """Run a service and restart unexpected failures with bounded backoff.

    ``factory`` must create a fresh coroutine for every attempt. Normal completion
    is intentional (for example disabled Telegram), and cancellation always stops
    the supervisor. ``registry`` is memory-only so recovery does not depend on the
    database that may have caused the failure. It contains no exception messages,
    model prompts, tokens, or feed URLs.
    """
    if any(not math.isfinite(value) or value < 0
           for value in (initial_delay, max_delay, stable_seconds)):
        raise ValueError('Service retry delays must be finite and nonnegative.')
    if initial_delay == 0:
        raise ValueError('Initial service retry delay must be positive.')
    if max_delay < initial_delay:
        raise ValueError('Maximum service retry delay must not be below the initial delay.')

    state = {
        'phase': 'starting',
        'restart_count': 0,
        'last_started_at': None,
        'last_failure_time': None,
        'last_failure_category': None,
        'next_restart_at': None,
        'stopped_reason': None,
    }
    registry[name] = dict(state)
    delay = initial_delay
    try:
        while True:
            started = monotonic()
            state.update(phase='running', last_started_at=_now().isoformat(),
                         next_restart_at=None, stopped_reason=None)
            registry[name] = dict(state)
            try:
                await factory()
            except CancelledError:
                raise
            except Exception as error:
                if monotonic() - started >= stable_seconds:
                    delay = initial_delay
                failed = _now()
                state.update(phase='retrying', last_failure_time=failed.isoformat(),
                             last_failure_category=type(error).__name__,
                             next_restart_at=(failed + dt.timedelta(seconds=delay)).isoformat())
                registry[name] = dict(state)
                log.warning('Background service %s failed: category=%s; retrying in %ss',
                            name, type(error).__name__, delay)
                await sleep(delay)
                state['restart_count'] += 1
                delay = min(max_delay, delay * 2)
            else:
                state.update(phase='stopped', stopped_reason='completed', next_restart_at=None)
                registry[name] = dict(state)
                return
    except CancelledError:
        state.update(phase='stopped', stopped_reason='cancelled', next_restart_at=None)
        registry[name] = dict(state)
        raise
