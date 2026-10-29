import asyncio
import logging
import time
from contextlib import asynccontextmanager

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


async def run_acks(targets: list[Notification]) -> tuple[list[dict], list[dict]]:
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
        [{"result": "acked", **a} for a in acked]
        + [{"result": "failed", **f} for f in failed]
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


@app.post("/ack-all")
async def ack_all():
    if lock.locked():
        return busy()
    started = time.monotonic()
    async with lock:
        try:
            items = await client.list_pending()
            if cfg.dry_run:
                return ok(would_ack=[i.to_dict() for i in items], pending_before=len(items), dry_run=True)
            acked, failed = await run_acks(items)
        except Mir3Error as e:
            return fail(FAILED, e.code, str(e))
        except Exception as e:
            log.exception("ack-all failed")
            return fail(FAILED, "mir3_error", str(e))
    return ack_response(acked, failed, len(items), started)


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


def run():
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level=cfg.log_level.lower())


if __name__ == "__main__":
    run()
