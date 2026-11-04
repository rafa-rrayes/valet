"""`valet-runner`: RunnerConfig.from_env(), then Runner(cfg).run() until SIGINT/SIGTERM, then a clean close()."""
import asyncio
import logging
import signal
import sys

from valet.shared.config import RunnerConfig

from .runner import Runner

log = logging.getLogger("valet.runner")


async def _serve(cfg: RunnerConfig) -> int:
    runner = Runner(cfg)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    run = asyncio.create_task(runner.run())
    stopping = asyncio.create_task(stop.wait())
    await asyncio.wait({run, stopping}, return_when=asyncio.FIRST_COMPLETED)
    log.info("shutting down")
    await runner.close()
    stopping.cancel()
    try:
        await run
    except RuntimeError as e:
        log.error("%s", e)
        return 1
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = RunnerConfig.from_env()
    except ValueError as e:
        sys.exit(f"valet-runner: {e}")
    sys.exit(asyncio.run(_serve(cfg)))


if __name__ == "__main__":
    main()
