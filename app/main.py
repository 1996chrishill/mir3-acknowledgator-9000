import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import uvicorn
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import config
from .history import History
from .mir3 import Mir3Client, Mir3Error, Notification

# Operation failures (login failed, MIR3 error, ack failed) return this instead of 500,
# so a failed operation can be told apart from a crashed web server.
FAILED = 424  # Failed Dependency

cfg = config.load()
logging.basicConfig(
    level=cfg.log_level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("api")

client = Mir3Client(cfg)
history = History(cfg.history_file)
lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await client.start()
    log.info("Listening on %s:%s (dry_run=%s)", cfg.host, cfg.port, cfg.dry_run)
    yield
    auto.shutdown()
    await client.stop()


app = FastAPI(title="mir3-acknowledgator-9000", lifespan=lifespan)

# Browser origins (e.g. the frontend) allowed to call the API. Unset = no CORS headers.
if cfg.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cfg.cors_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )


def ok(**body) -> JSONResponse:
    return JSONResponse({"status": "success", **body}, status_code=200)


def fail(code: int, error: str, detail: str, **body) -> JSONResponse:
    return JSONResponse({"status": "failed", "error": error, "detail": detail, **body}, status_code=code)


def busy() -> JSONResponse:
    return fail(409, "busy", "Another operation is in progress")


async def run_acks(targets: list[Notification], source: str = "manual") -> tuple[list[dict], list[dict]]:
    acked, failed = [], []
    for n in targets:
        try:
            await client.acknowledge(n)
            still = {p.event_hist_id for p in await client.list_pending()}
            if n.event_hist_id in still:
                raise Mir3Error("Still pending after submitting ack")
            acked.append(n.to_dict())
            log.info("ACKED  %s", n.title)
        except Exception as e:
            failed.append({**n.to_dict(), "error": str(e)})
            log.error("FAILED %s: %s", n.title, e)
    history.add(
        [{"result": "acked", "source": source, **a} for a in acked]
        + [{"result": "failed", "source": source, **f} for f in failed]
    )
    return acked, failed


def ack_response(acked, failed, pending_before, started) -> JSONResponse:
    body = dict(
        acked=acked,
        failed=failed,
        pending_before=pending_before,
        dry_run=False,
        duration_s=round(time.monotonic() - started, 2),
    )
    if failed:
        return fail(FAILED, "ack_failed", f"{len(failed)} of {len(acked) + len(failed)} acks failed", **body)
    return ok(**body)


@app.get("/health")
async def health():
    return {"status": "ok", "busy": lock.locked(), "dry_run": cfg.dry_run}


@app.get("/history")
async def get_history(limit: int | None = Query(None, ge=1)):
    items = history.recent(limit)
    return ok(count=len(items), history=items)


@app.get("/pending")
async def pending():
    if lock.locked():
        return busy()
    async with lock:
        try:
            items = await client.list_pending()
        except Mir3Error as e:
            return fail(FAILED, e.code, str(e))
        except Exception as e:
            log.exception("pending failed")
            return fail(FAILED, "mir3_error", str(e))
    return ok(count=len(items), pending=[i.to_dict() for i in items])


async def ack_all_core(source: str) -> JSONResponse:
    """Ack every pending notification. Caller must hold `lock`."""
    started = time.monotonic()
    try:
        items = await client.list_pending()
        if cfg.dry_run:
            return ok(would_ack=[i.to_dict() for i in items], pending_before=len(items), dry_run=True)
        acked, failed = await run_acks(items, source)
    except Mir3Error as e:
        return fail(FAILED, e.code, str(e))
    except Exception as e:
        log.exception("ack-all failed")
        return fail(FAILED, "mir3_error", str(e))
    return ack_response(acked, failed, len(items), started)


@app.post("/ack-all")
async def ack_all():
    if lock.locked():
        return busy()
    async with lock:
        return await ack_all_core("manual")


@app.post("/ack/{event_hist_id}")
async def ack_one(event_hist_id: str):
    if lock.locked():
        return busy()
    started = time.monotonic()
    async with lock:
        try:
            items = await client.list_pending()
            match = [i for i in items if i.event_hist_id == event_hist_id]
            if not match:
                return fail(404, "not_pending", f"{event_hist_id} is not in pending notifications")
            if cfg.dry_run:
                return ok(would_ack=[match[0].to_dict()], pending_before=len(items), dry_run=True)
            acked, failed = await run_acks(match)
        except Mir3Error as e:
            return fail(FAILED, e.code, str(e))
        except Exception as e:
            log.exception("ack failed")
            return fail(FAILED, "mir3_error", str(e))
    return ack_response(acked, failed, len(items), started)


# ---------- auto-ack job (in memory only: off after every restart) ----------

def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class AutoAck:
    """Runs ack-all every `interval_s` until stopped or `max_duration_s` passes (0 = no limit).

    First run happens right away. Off at boot; only POST /auto/start turns it on.
    """

    def __init__(self, interval_s: int, max_duration_s: int):
        self.interval_s = interval_s
        self.max_duration_s = max_duration_s
        self._task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self._deadline: float | None = None  # loop.time() when the job stops itself
        self.started_at: str | None = None
        self.stops_at: str | None = None
        self.stopped_at: str | None = None
        self.stop_reason: str | None = None  # "user" | "max_duration"
        self.last_run_at: str | None = None
        self.next_run_at: str | None = None
        self.last_result: dict | None = None
        self.runs = 0
        self.running = False

    @property
    def enabled(self) -> bool:
        return bool(self._task and not self._task.done() and self._stop and not self._stop.is_set())

    def status(self) -> dict:
        on = self.enabled
        return dict(
            enabled=on,
            running=self.running,
            interval_s=self.interval_s,
            max_duration_s=self.max_duration_s,
            started_at=self.started_at if on else None,
            stops_at=self.stops_at if on else None,
            stopped_at=None if on else self.stopped_at,
            stop_reason=None if on else self.stop_reason,
            last_run_at=self.last_run_at,
            next_run_at=self.next_run_at if on else None,
            runs=self.runs,
            last_result=self.last_result,
        )

    def start(self) -> None:
        if self.enabled:
            return
        self._stop = asyncio.Event()
        now = datetime.now().astimezone()
        self.started_at, self.runs, self.next_run_at = now.isoformat(timespec="seconds"), 0, None
        self.stopped_at = self.stop_reason = None
        if self.max_duration_s:
            self._deadline = asyncio.get_running_loop().time() + self.max_duration_s
            self.stops_at = (now + timedelta(seconds=self.max_duration_s)).isoformat(timespec="seconds")
        else:
            self._deadline = self.stops_at = None
        self._task = asyncio.create_task(self._loop(self._stop))

    def stop(self, reason: str = "user") -> None:
        # a run already in progress finishes; no new runs start
        if self.enabled:
            self.stop_reason, self.stopped_at = reason, _now()
        if self._stop:
            self._stop.set()
        self.next_run_at = None

    def shutdown(self) -> None:
        self.stop()
        if self._task and not self._task.done():
            self._task.cancel()

    def _remaining(self) -> float | None:
        return None if self._deadline is None else self._deadline - asyncio.get_running_loop().time()

    async def _loop(self, stop: asyncio.Event) -> None:
        log.info(
            "Auto-ack started (every %ss, max %s)",
            self.interval_s, f"{self.max_duration_s}s" if self.max_duration_s else "unlimited",
        )
        while not stop.is_set():
            await self._run_once(stop)
            if stop.is_set():
                break
            wait = self.interval_s
            left = self._remaining()
            if left is not None:
                if left <= wait:
                    # no run fits before the limit: sleep until it, then stop
                    self.next_run_at = None
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=max(0, left))
                    except asyncio.TimeoutError:
                        pass
                    if not stop.is_set():
                        log.info("Auto-ack reached max duration (%ss)", self.max_duration_s)
                        self.stop("max_duration")
                    break
            self.next_run_at = (datetime.now().astimezone() + timedelta(seconds=wait)).isoformat(timespec="seconds")
            try:
                await asyncio.wait_for(stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
        log.info("Auto-ack stopped (%s)", self.stop_reason)

    async def _run_once(self, stop: asyncio.Event) -> None:
        self.next_run_at = None
        async with lock:  # waits for any manual call to finish
            if stop.is_set():
                return
            self.running = True
            try:
                resp = await ack_all_core("auto")
            finally:
                self.running = False
        body = json.loads(resp.body)
        self.runs += 1
        self.last_run_at = _now()
        self.last_result = {"code": resp.status_code, **body}
        log.info(
            "Auto-ack run %s: %s (acked=%s failed=%s would_ack=%s)",
            self.runs, resp.status_code,
            len(body.get("acked", [])), len(body.get("failed", [])), len(body.get("would_ack", [])),
        )


auto = AutoAck(cfg.auto_interval_s, cfg.auto_max_duration_s)


@app.get("/auto")
async def auto_status():
    return ok(**auto.status())


@app.post("/auto/start")
async def auto_start():
    auto.start()
    return ok(**auto.status())


@app.post("/auto/stop")
async def auto_stop():
    auto.stop()
    return ok(**auto.status())


def run():
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level=cfg.log_level.lower())


if __name__ == "__main__":
    run()
