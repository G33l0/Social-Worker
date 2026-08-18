"""HTTP control API for distributed operation.

The console drives one controller on one machine.  This API exposes the same
controls over HTTP so a test can be started from CI, watched from a dashboard,
or driven across several machines, and so regional worker agents can register
and report heartbeats to a central controller.

Security posture: the API is off by default.  When enabled it binds loopback
unless the operator explicitly changes the host, and it refuses to bind a
non-loopback address without an API token.  The token is compared with a
constant-time check and is never logged or written into a run's configuration
snapshot.
"""

from __future__ import annotations

import hmac
from typing import Any, Callable

from pydantic import BaseModel, Field

from app.core.controller import Controller
from app.utils.logger import get_logger

LOGGER = get_logger("api")


class StartRequest(BaseModel):
    """Body for ``POST /test/start``."""

    label: str = "api"
    dry_run: bool = False
    force: bool = False


class WorkerRegistration(BaseModel):
    """A regional worker agent announcing itself to the controller."""

    worker_id: str
    location_key: str
    runner: str = "remote"
    capacity: int = Field(1, ge=1, le=1_000)
    version: str = ""


class HeartbeatReport(BaseModel):
    """Periodic liveness report from a registered agent."""

    status: str = "ACTIVE"
    action: str = ""
    sessions_completed: int = 0
    errors: int = 0


class ApiUnavailableError(RuntimeError):
    """Raised when FastAPI is not installed."""


def create_app(controller: Controller, *, token: str = "") -> Any:
    """Build the FastAPI application bound to *controller*."""
    try:
        from fastapi import Depends, FastAPI, Header, HTTPException
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise ApiUnavailableError(
            "The control API needs FastAPI: pip install fastapi uvicorn") from exc

    app = FastAPI(title="Social Worker Control API", version="1.0.0",
                  docs_url="/docs", redoc_url=None)

    async def authorize(authorization: str = Header(default="")) -> None:
        """Bearer-token check; a blank configured token disables auth."""
        if not token:
            return
        supplied = authorization.removeprefix("Bearer ").strip()
        if not hmac.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="Invalid or missing API token")

    guard: list[Any] = [Depends(authorize)]

    @app.get("/health")
    def health() -> dict[str, Any]:
        """Liveness probe (unauthenticated)."""
        return {"status": "ok", "state": controller.state,
                "test_run_id": controller.test_run_id}

    @app.get("/status", dependencies=guard)
    def status() -> dict[str, Any]:
        """Full live status: the same snapshot the dashboard renders."""
        return controller.status()

    @app.get("/verdict", dependencies=guard)
    def verdict(test_run_id: str = "") -> dict[str, Any]:
        """Acceptance verdict for a run (defaults to the current one)."""
        target = test_run_id or controller.test_run_id
        if not target:
            raise HTTPException(status_code=404, detail="No test run to score")
        return controller.evaluate_thresholds(target).to_dict()

    @app.post("/test/start", dependencies=guard)
    async def start(request: StartRequest) -> dict[str, Any]:
        """Start a load test."""
        if controller.is_running:
            raise HTTPException(status_code=409, detail="A test run is already active")
        try:
            test_run_id = await controller.start(label=request.label,
                                                 dry_run=request.dry_run,
                                                 force=request.force)
        except Exception as exc:  # authorization or preflight failure
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"test_run_id": test_run_id, "state": controller.state}

    @app.post("/test/pause", dependencies=guard)
    async def pause() -> dict[str, Any]:
        """Pause dispatching."""
        await controller.pause()
        return {"state": controller.state}

    @app.post("/test/resume", dependencies=guard)
    async def resume() -> dict[str, Any]:
        """Resume dispatching."""
        await controller.resume()
        return {"state": controller.state}

    @app.post("/test/stop", dependencies=guard)
    async def stop() -> dict[str, Any]:
        """Graceful stop: in-flight sessions finish."""
        await controller.stop()
        return {"state": controller.state, "verdict": _verdict_label(controller)}

    @app.post("/test/stop-all", dependencies=guard)
    async def stop_all() -> dict[str, Any]:
        """Emergency stop: cancel every queued and running session now."""
        result = await controller.stop_all(reason="control API")
        return {"state": controller.state, **result}

    @app.post("/locations/{location_key}/stop", dependencies=guard)
    async def stop_location(location_key: str) -> dict[str, Any]:
        """Stop one geographic location."""
        return await controller.stop_location(location_key)

    @app.post("/workers/{worker_id}/stop", dependencies=guard)
    async def stop_worker(worker_id: str) -> dict[str, Any]:
        """Stop one worker."""
        return await controller.stop_worker(worker_id)

    @app.get("/locations", dependencies=guard)
    def locations() -> list[dict[str, Any]]:
        """Configured locations and their ceilings."""
        return controller.locations.summary_rows()

    @app.get("/workers", dependencies=guard)
    def workers() -> list[dict[str, Any]]:
        """Live worker rows."""
        return controller.pool.rows() if controller.pool else []

    @app.post("/workers/register", dependencies=guard)
    def register(registration: WorkerRegistration) -> dict[str, Any]:
        """Register a regional worker agent with the controller."""
        controller.heartbeat.register(registration.worker_id,
                                      location_key=registration.location_key,
                                      runner=registration.runner)
        LOGGER.info("Registered agent %s for %s (capacity %d)",
                    registration.worker_id, registration.location_key,
                    registration.capacity)
        return {"registered": registration.worker_id,
                "heartbeat_interval_seconds":
                    controller.settings.controller.heartbeat_interval_seconds}

    @app.post("/workers/{worker_id}/heartbeat", dependencies=guard)
    def heartbeat(worker_id: str, report: HeartbeatReport) -> dict[str, Any]:
        """Record a heartbeat from a registered agent."""
        controller.heartbeat.beat(worker_id, status=report.status,
                                  action=report.action)
        return {"worker_id": worker_id, "acknowledged": True,
                "stop_requested": controller.state in {"STOPPING", "STOPPED"}}

    @app.get("/heartbeats", dependencies=guard)
    def heartbeats() -> dict[str, Any]:
        """Every known agent plus the ones that have gone quiet."""
        return {"agents": controller.heartbeat.snapshot(),
                "stale": [beat.worker_id for beat in controller.heartbeat.stale()]}

    @app.get("/runs", dependencies=guard)
    def runs(limit: int = 20) -> list[dict[str, Any]]:
        """Recent test runs."""
        from app.database.repository import Repository

        return Repository(controller.database).list_test_runs(limit=limit)

    @app.get("/reports/{test_run_id}/{category}", dependencies=guard)
    def report(test_run_id: str, category: str) -> Any:
        """Render one report category as JSON."""
        try:
            return controller.reports.build(category, test_run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return app


def _verdict_label(controller: Controller) -> str:
    return controller.verdict.label if controller.verdict else "NOT EVALUATED"


def serve(controller: Controller, *, host: str = "", port: int = 0,
          token: str = "", run: Callable[..., None] | None = None) -> None:
    """Run the control API with uvicorn (blocking)."""
    settings = controller.settings.controller
    host = host or settings.host
    port = port or settings.port
    token = token or settings.api_token

    if host not in {"127.0.0.1", "localhost", "::1"} and not token:
        raise ValueError(
            f"Refusing to expose the control API on {host} without an API token. "
            "Set controller.api_token (or CONTROLLER_API_TOKEN) first.")

    application = create_app(controller, token=token)
    if run is not None:  # injection point for tests
        run(application, host=host, port=port)
        return

    import uvicorn

    LOGGER.info("Control API listening on http://%s:%d (auth %s)", host, port,
                "enabled" if token else "disabled")
    uvicorn.run(application, host=host, port=port, log_level="warning")
