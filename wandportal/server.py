"""Web console: live IR view, spell training, threshold tuning."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from .engine import Engine

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

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return HTMLResponse((WEB_DIR / "index.html").read_text())

    @app.get("/stream.mjpg")
    async def stream():
        interval = 1.0 / max(1, engine.cfg.server.stream_fps)
        quality = engine.cfg.server.stream_quality

        async def frames():
            while True:
                jpg = engine.jpeg(quality)
                if jpg:
                    yield (
                        b"--frame\r\nContent-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n"
                    )
                await asyncio.sleep(interval)

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
        return engine.status()

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
