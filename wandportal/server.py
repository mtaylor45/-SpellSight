"""Web console: live IR view, spell training, threshold tuning."""

from __future__ import annotations

import asyncio
import secrets
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from . import config as config_module
from .engine import Engine
from .spells import BY_ID
from .tracker import clean_band, sweep_thresholds

WEB_DIR = Path(__file__).parent / "web"


class ModeRequest(BaseModel):
    mode: str
    spell_id: str | None = None


class TuneRequest(BaseModel):
    """Bounds are deliberately wide but finite.

    These come from a slider on a phone, and an out-of-range value does not fail
    loudly — it silently stops the wand being detected at all, which looks like
    broken hardware. FastAPI turns a violation into a 422 naming the field.
    """

    threshold: int | None = Field(None, ge=1, le=254)
    min_area: float | None = Field(None, gt=0, le=10_000)
    max_area: float | None = Field(None, gt=0, le=1_000_000)
    lost_frames: int | None = Field(None, ge=1, le=300)
    min_path_length: float | None = Field(None, ge=0, le=10_000)
    cooldown: float | None = Field(None, ge=0, le=600)
    min_confidence: float | None = Field(None, ge=0.0, le=1.0)
    min_margin: float | None = Field(None, ge=0.0, le=1.0)


def create_app(engine: Engine) -> FastAPI:
    app = FastAPI(title="Wand Portal", docs_url="/api/docs", openapi_url="/api/openapi.json")

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        """Gate the data behind a token, when one is configured.

        The console HTML itself stays open so a browser can load the page and
        prompt for the token — it carries no camera data. Everything that does,
        the API and the stream, is gated.

        The query parameter exists because an <img src> cannot set an
        Authorization header, and the stream has to be loadable by one.
        """
        token = engine.cfg.server.auth_token
        path = request.url.path
        if token and (path.startswith("/api/") or path == "/stream.mjpg"):
            header = request.headers.get("authorization", "")
            supplied = header[7:] if header.lower().startswith("bearer ") else ""
            supplied = supplied or request.query_params.get("t", "")
            # Constant time: a timing oracle on a LAN is a real way to recover
            # a short token.
            if not supplied or not secrets.compare_digest(supplied, token):
                return JSONResponse(
                    {"detail": "missing or invalid token — pass ?t=<token> or "
                               "an Authorization: Bearer header"},
                    status_code=401,
                )
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return HTMLResponse((WEB_DIR / "index.html").read_text())

    @app.get("/stream.mjpg")
    async def stream():
        mode = engine.cfg.server.stream_mode
        if mode == "off":
            raise HTTPException(
                404,
                "the video stream is disabled (server.stream_mode: off). Training "
                "and tuning still work from the trace thumbnails.",
            )
        interval = 1.0 / max(1, engine.cfg.server.stream_fps)
        quality = engine.cfg.server.stream_quality

        async def frames():
            engine.viewers += 1
            try:
                while True:
                    jpg = engine.jpeg(quality)
                    if jpg:
                        yield (
                            b"--frame\r\nContent-Type: image/jpeg\r\n"
                            b"Content-Length: " + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n"
                        )
                    await asyncio.sleep(interval)
            finally:
                engine.viewers = max(0, engine.viewers - 1)

        return StreamingResponse(
            frames(),
            media_type="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/status")
    async def status():
        return engine.status()

    @app.post("/api/mode")
    async def set_mode(req: ModeRequest):
        try:
            engine.set_mode(req.mode, req.spell_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return engine.status()

    @app.post("/api/tune")
    async def tune(req: TuneRequest):
        t, r = engine.cfg.tracker, engine.cfg.recognizer
        # Each field can be in range while the pair is nonsense. An inverted
        # area window matches no blob at all, so reject it rather than apply it.
        lo = req.min_area if req.min_area is not None else t.min_area
        hi = req.max_area if req.max_area is not None else t.max_area
        if lo >= hi:
            raise HTTPException(
                422, f"min_area ({lo}) must be below max_area ({hi}); "
                     "an inverted area window matches nothing"
            )
        for name in ("threshold", "min_area", "max_area", "lost_frames",
                     "min_path_length", "cooldown"):
            value = getattr(req, name)
            if value is not None:
                setattr(t, name, value)
        for name in ("min_confidence", "min_margin"):
            value = getattr(req, name)
            if value is not None:
                setattr(r, name, value)
        engine.unsaved_tuning = True
        return engine.status()

    @app.post("/api/config/save")
    async def save_config():
        """Write the live tracker and recognizer values back to config.yaml.

        Tuning is otherwise lost on restart, which is a bad surprise after an
        hour of threshold work — and the hour of threshold work is the next
        thing to happen on this project.
        """
        t, r = engine.cfg.tracker, engine.cfg.recognizer
        updates = {
            "tracker": {k: getattr(t, k) for k in (
                "threshold", "min_area", "max_area", "max_jump", "lost_frames",
                "min_points", "min_path_length", "max_duration", "cooldown",
                "smoothing", "blur")},
            "recognizer": {k: getattr(r, k) for k in ("min_confidence", "min_margin")},
        }
        try:
            result = config_module.save_values(engine.cfg.config_path, updates)
        except OSError as exc:
            # compose mounts config.yaml read-only; that is a 409, not a 500.
            raise HTTPException(
                409,
                f"could not write {engine.cfg.config_path}: {exc.strerror or exc}. "
                "The file is probably mounted read-only (docker-compose mounts it :ro) "
                "or owned by another user.",
            )
        engine.unsaved_tuning = False
        shadowed = config_module.env_shadowed(updates)
        return {
            **result,
            # Saving a value an env var shadows looks like the save failed: the
            # file changes, the behaviour does not. Say so rather than hide it.
            "shadowed_by_env": shadowed,
            "note": ("These keys are overridden by environment variables and will not "
                     "take effect on restart: " + ", ".join(shadowed)) if shadowed else "",
        }

    @app.get("/api/tune/sweep")
    def tune_sweep(lo: int = 60, hi: int = 250, step: int = 5):
        """Blob counts across a range of cutoffs, on the newest frame.

        Deliberately a sync def: FastAPI runs it in a threadpool, so the ~40
        thresholding passes neither block the event loop nor the capture thread,
        which reads from the camera independently.
        """
        if not 1 <= lo < hi <= 254:
            raise HTTPException(422, f"need 1 <= lo < hi <= 254, got lo={lo} hi={hi}")
        if not 1 <= step <= 64:
            raise HTTPException(422, f"step {step} must be in 1..64")
        _, frame = engine.camera.read()
        if frame is None:
            raise HTTPException(503, "no frame yet — the camera has not delivered one")
        t = engine.cfg.tracker
        started = time.perf_counter()
        rows = sweep_thresholds(frame, lo=lo, hi=hi, step=step,
                                min_area=t.min_area, max_area=t.max_area, blur=t.blur)
        return {
            "sweep": rows,
            "band": clean_band(rows),
            "current": t.threshold,
            "took_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    @app.get("/api/samples/{spell_id}")
    async def samples(spell_id: str):
        traces = engine.recognizer.points_for(spell_id)
        return {"spell_id": spell_id, "count": len(traces)}

    @app.get("/api/samples/{spell_id}/{index}.png")
    async def sample_png(spell_id: str, index: int):
        png = engine.trace_png(spell_id, index)
        if png is None:
            raise HTTPException(404, "no such sample")
        return Response(png, media_type="image/png",
                        headers={"Cache-Control": "no-store"})

    @app.delete("/api/samples/{spell_id}/{index}")
    async def delete_sample(spell_id: str, index: int):
        if not engine.recognizer.delete_sample(spell_id, index):
            raise HTTPException(404, "no such sample")
        return {"ok": True, "count": len(engine.recognizer.points_for(spell_id))}

    @app.delete("/api/samples/{spell_id}")
    async def clear_spell(spell_id: str):
        engine.recognizer.clear_spell(spell_id)
        return {"ok": True}

    @app.get("/api/rejections")
    async def rejections():
        """The last rejected casts, with the scores that rejected them."""
        return {"rejections": [
            {k: v for k, v in r.items() if k != "points"} | {"points": len(r["points"])}
            for r in engine.rejections
        ]}

    @app.get("/api/rejections/{index}.png")
    async def rejection_png(index: int):
        png = engine.rejection_png(index)
        if png is None:
            raise HTTPException(404, "no such rejection")
        return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/api/templates/export")
    async def export_templates():
        """The trained templates, as a plain JSON document.

        The README promises templates are copyable between installs; this is the
        endpoint that makes that true without reaching into the filesystem.
        """
        return {
            "version": 1,
            "resample_points": engine.cfg.recognizer.resample_points,
            "rotation_invariant": engine.cfg.recognizer.rotation_invariant,
            "samples": {sid: engine.recognizer.points_for(sid)
                        for sid in engine.recognizer.counts()},
        }

    @app.post("/api/templates/import")
    async def import_templates(payload: dict = Body(...), replace: bool = False):
        """Import templates exported from another install.

        Refuses a document normalized differently: matching 64-point templates
        against 32-point ones would silently wreck recognition rather than fail.
        """
        samples = payload.get("samples")
        if not isinstance(samples, dict) or not samples:
            raise HTTPException(422, "payload needs a non-empty 'samples' mapping")
        theirs = int(payload.get("resample_points", engine.cfg.recognizer.resample_points))
        if theirs != engine.cfg.recognizer.resample_points:
            raise HTTPException(
                422,
                f"templates were normalized to {theirs} points but this install uses "
                f"{engine.cfg.recognizer.resample_points}; importing them would quietly "
                "degrade recognition",
            )
        unknown = [sid for sid in samples if sid not in BY_ID]
        if unknown:
            raise HTTPException(422, f"unknown spell id(s): {', '.join(sorted(unknown))}")

        imported = 0
        for sid, traces in samples.items():
            if replace:
                engine.recognizer.clear_spell(sid)
            for trace in traces:
                points = [(float(p[0]), float(p[1])) for p in trace]
                if len(points) >= 2:
                    engine.recognizer.add_sample(sid, points)
                    imported += 1
        return {"imported": imported, "counts": engine.recognizer.counts()}

    @app.post("/api/self-test")
    async def self_test():
        return engine.recognizer.self_test(enabled=engine.spell_ids)

    @app.post("/api/cast/{spell_id}")
    async def manual_cast(spell_id: str):
        """Fire a spell by hand — for wiring up Home Assistant before training."""
        spell = engine.by_id.get(spell_id)
        if spell is None:
            raise HTTPException(404, "spell not enabled")
        engine.mqtt.cast(spell, 1.0, 0.0)
        return {"ok": True, "spell": spell_id, "mqtt": engine.mqtt.connected}

    @app.post("/api/discovery")
    async def republish_discovery():
        engine.mqtt.publish_discovery()
        return {"ok": True}

    @app.exception_handler(ValueError)
    async def value_error(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    return app
