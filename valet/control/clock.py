"""Time for the control plane's always-on parts (the scheduler, the prompt queue, wake-ups): one object, so tests
can move time on instead of waiting for it."""
import asyncio
import contextlib
import time


class Clock:
    """Real time. now(): unix seconds. sleep(seconds, wake): returns after `seconds`, or as soon as `wake` is set."""

    def now(self) -> float:
        return time.time()

    async def sleep(self, seconds: float, wake: asyncio.Event | None = None) -> None:
        if wake is None:
            await asyncio.sleep(max(0.0, seconds))
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(wake.wait(), max(0.0, seconds))


class ManualClock(Clock):
    """A clock tests set (`t`, or advance()); sleeping waits in real time for at most `tick` seconds, so a loop
    driven by it notices a change to `t` quickly."""

    def __init__(self, t: float, tick: float = 0.02) -> None:
        self.t = t
        self.tick = tick

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t

    async def sleep(self, seconds: float, wake: asyncio.Event | None = None) -> None:
        await super().sleep(min(seconds, self.tick), wake)
