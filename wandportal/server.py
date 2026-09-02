"""The web console's HTTP API.

FastAPI, serving one hand-written HTML file plus an MJPEG stream and a small
JSON API. The stream generator runs in FastAPI's threadpool rather than on the
event loop, so a slow client can't stall anything else.

SPEC.md R3.3 adds authentication and an on-demand stream mode. Until then this
binds to the LAN unauthenticated, which is called out in the README.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import cv2
from fastapi import Body, FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse, StreamingResponse

from .engine import Engine, render_trace
from .spells import SPELLS, SPELLS_BY_ID

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"
BOUNDARY = "wandframe"


def _encode(image, quality: int = 70) -> bytes | None:
    ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return buffer.tobytes() if ok else None


def create_app(engine: Engine) -> FastAPI:
    """Build the FastAPI app around a running engine."""
    app = FastAPI(title="Wand Portal", version="1.0.0", docs_url=None, redoc_url=None)
    quality = engine.config.server.jpeg_quality

    # ---- console ---------------------------------------------------------

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/stream.mjpg")
    def stream(mode: str = Query("cast", pattern="^(cast|tune)$")) -> StreamingResponse:
        def frames():
            interval = 1.0 / 25.0
            while True:
                start = time.monotonic()
                image = engine.frame(mode)
                if image is not None:
                    jpeg = _encode(image, quality)
                    if jpeg:
                        yield (
                            b"--" + BOUNDARY.encode() + b"\r\n"
                            b"Content-Type: image/jpeg\r\n"
                            b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                            + jpeg + b"\r\n"
                        )
                elapsed = time.monotonic() - start
                time.sleep(max(0.0, interval - elapsed))

        return StreamingResponse(
            frames(), media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}"
        )

    @app.get("/frame.jpg")
    def frame(mode: str = Query("cast", pattern="^(cast|tune)$")) -> Response:
        """A single frame. The fallback when a client can't hold an MJPEG stream open."""
        image = engine.frame(mode)
        if image is None:
            raise HTTPException(status_code=503, detail="no frame yet")
        jpeg = _encode(image, quality)
        if not jpeg:
            raise HTTPException(status_code=500, detail="encode failed")
        return Response(content=jpeg, media_type="image/jpeg")

    # ---- status and spells ----------------------------------------------

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return engine.status()

    @app.get("/api/spells")
    def spells() -> dict[str, Any]:
        with engine.lock:
            counts = {s: engine.recognizer.sample_count(s) for s in engine.recognizer.trained_spells()}
        return {
            "spells": [
                {
                    "id": spell.id,
                    "name": spell.name,
                    "effect": spell.effect,
                    "samples": counts.get(spell.id, 0),
                    "trained": counts.get(spell.id, 0) > 0,
                }
                for spell in SPELLS
            ],
            "trained": sorted(counts),
        }

    @app.get("/api/spells/{spell_id}/samples")
    def samples(spell_id: str) -> dict[str, Any]:
        _require_spell(spell_id)
        with engine.lock:
            stored = engine.recognizer.samples.get(spell_id, [])
            return {
                "spell": spell_id,
                "samples": [
                    {"id": s.id, "created": s.created, "points": len(s.points)} for s in stored
                ],
            }

    @app.get("/api/spells/{spell_id}/samples/{sample_id}/trace.png")
    def sample_trace(spell_id: str, sample_id: str) -> Response:
        _require_spell(spell_id)
        with engine.lock:
            for sample in engine.recognizer.samples.get(spell_id, []):
                if sample.id == sample_id:
                    points = list(sample.points)
                    break
            else:
                raise HTTPException(status_code=404, detail="no such sample")
        ok, buffer = cv2.imencode(".png", render_trace(points))
        if not ok:
            raise HTTPException(status_code=500, detail="encode failed")
        return Response(content=buffer.tobytes(), media_type="image/png")

    @app.delete("/api/spells/{spell_id}/samples/{sample_id}")
    def delete_sample(spell_id: str, sample_id: str) -> dict[str, Any]:
        _require_spell(spell_id)
        with engine.lock:
            removed = engine.recognizer.delete_sample(spell_id, sample_id)
        if removed:
            engine.save_templates()
        if not removed:
            raise HTTPException(status_code=404, detail="no such sample")
        return {"deleted": sample_id, "remaining": engine.recognizer.sample_count(spell_id)}

    @app.delete("/api/spells/{spell_id}/samples")
    def clear_samples(spell_id: str) -> dict[str, Any]:
        _require_spell(spell_id)
        with engine.lock:
            removed = engine.recognizer.clear_spell(spell_id)
        engine.save_templates()
        return {"spell": spell_id, "removed": removed}

    @app.post("/api/spells/{spell_id}/test")
    def test_spell(spell_id: str) -> dict[str, Any]:
        _require_spell(spell_id)
        published = engine.test_cast(spell_id)
        return {"spell": spell_id, "published": published}

    # ---- training --------------------------------------------------------

    @app.post("/api/train/start")
    def train_start(payload: dict = Body(...)) -> dict[str, Any]:
        spell_id = str(payload.get("spell", ""))
        _require_spell(spell_id)
        engine.start_training(spell_id)
        return {"training": spell_id}

    @app.post("/api/train/stop")
    def train_stop() -> dict[str, Any]:
        engine.stop_training()
        return {"training": None}

    # ---- tuning ----------------------------------------------------------

    @app.get("/api/tuning")
    def get_tuning() -> dict[str, Any]:
        return engine.tuning()

    @app.post("/api/tuning")
    def post_tuning(payload: dict = Body(...)) -> dict[str, Any]:
        try:
            changed = engine.apply_tuning(payload)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"bad tuning value: {exc}") from exc
        return {
            "changed": changed,
            "tuning": engine.tuning(),
            # SPEC.md R3.4 adds persistence; until then say so plainly rather
            # than letting an hour of tuning vanish on the next restart.
            "persisted": False,
        }

    @app.get("/api/separation")
    def separation() -> dict[str, Any]:
        with engine.lock:
            return engine.recognizer.separation()

    # ---- Home Assistant --------------------------------------------------

    @app.post("/api/discovery")
    def discovery(payload: dict = Body(default={})) -> dict[str, Any]:
        requested = payload.get("spells")
        if requested:
            targets = [s for s in requested if s in SPELLS_BY_ID]
        else:
            with engine.lock:
                targets = engine.recognizer.trained_spells()
        published = engine.mqtt.publish_discovery(targets)
        return {"published": published, "spells": targets, "connected": engine.mqtt.connected}

    @app.delete("/api/discovery")
    def remove_discovery(payload: dict = Body(default={})) -> dict[str, Any]:
        requested = payload.get("spells")
        targets = [s for s in requested if s in SPELLS_BY_ID] if requested else None
        removed = engine.mqtt.remove_discovery(targets)
        return {"removed": removed}

    # ---- events ----------------------------------------------------------

    @app.get("/api/events")
    def events(limit: int = Query(25, ge=1, le=200)) -> dict[str, Any]:
        with engine.lock:
            items = list(engine.events)[:limit]
        return {"events": items}

    def _require_spell(spell_id: str) -> None:
        if spell_id not in SPELLS_BY_ID:
            raise HTTPException(status_code=404, detail=f"unknown spell {spell_id!r}")

    return app
