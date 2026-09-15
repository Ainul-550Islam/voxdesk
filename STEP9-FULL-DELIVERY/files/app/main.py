import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api.auth_routes import router as auth_router
from app.api.appointment_routes import (
    calendar_router,
    router as appointment_router,
)
from app.api.analytics_routes import router as analytics_router
from app.api.billing_routes import router as billing_router
from app.api.calendar_webhook_routes import router as calendar_webhook_router
from app.api.crm_webhook_routes import router as crm_webhook_router
from app.api.integration_routes import router as integration_router
from app.api.knowledge_routes import router as knowledge_router
from app.api.routes import router as api_router
from app.api.team_routes import router as team_router
from app.core.config import settings
from app.core.logging import log
from app.db.models import Base
from app.db.session import get_engine
from app.channels.messaging import router as channels_router
from app.telephony.twilio_handler import router as telephony_router

# Observability: Sentry error reporting is optional and off unless a DSN is
# configured. Initialised at import time so it covers startup failures too.
if settings.sentry_dsn:
    import sentry_sdk

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.app_env,
        # Keep a small trace sample in production; none in dev/test.
        traces_sample_rate=0.1 if settings.is_production else 0.0,
        send_default_pii=False,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Refuse to serve traffic with an insecure configuration. In development
    # the same problems are logged as warnings so the app stays runnable.
    problems = settings.validate_security()
    if problems:
        if settings.is_production:
            raise RuntimeError(
                "Insecure configuration, refusing to start: " + "; ".join(problems)
            )
        for problem in problems:
            log.warning("config.insecure", problem=problem)

    engine = get_engine()
    if not settings.is_production:
        # Development/test bootstrap: create any missing tables so the app is
        # usable without running migrations. Production never does this --
        # Alembic is the sole schema owner there, and create_all would build
        # schema outside the migration history (see alembic/versions/).
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    # STEP 7: make sure the plan catalogue exists, and check that every active
    # priced plan has a provider price id.
    #
    # Seeding lives here rather than in a migration because prices are
    # business data that changes: repricing should be an operator action
    # against a running system, not a schema change. `sync_seed_plans` never
    # overwrites a price an operator has edited.
    #
    # The configuration check is requirement 6 -- a plan with no price id
    # fails at boot rather than at checkout, where the failure would be in
    # front of a customer holding a credit card.
    from app.billing.plans import configuration_problems, list_plans, sync_seed_plans
    from app.db.session import get_sessionmaker

    maker = get_sessionmaker()
    async with maker() as session:
        await sync_seed_plans(session)
        plan_problems = configuration_problems(
            await list_plans(session, active_only=True),
            is_production=settings.is_production,
        )
    if plan_problems:
        if settings.is_production and settings.billing_provider == "stripe":
            raise RuntimeError(
                "Billing is misconfigured, refusing to start: "
                + "; ".join(plan_problems)
            )
        for problem in plan_problems:
            log.warning("billing.plan_misconfigured", problem=problem)

    log.info("voxdesk.started")
    yield
    await engine.dispose()


def _api_docs_config() -> dict[str, str | None]:
    """Swagger / ReDoc / OpenAPI are developer surfaces.

    In production they expose the full API schema and an interactive
    "try it out" console, so they are disabled there. Development keeps the
    FastAPI defaults.
    """
    if settings.is_production:
        return {"docs_url": None, "redoc_url": None, "openapi_url": None}
    return {}


app = FastAPI(
    title="VoxDesk",
    version="0.4.0",
    lifespan=lifespan,
    **_api_docs_config(),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,   # never "*" once cookies are in play
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(telephony_router)
app.include_router(channels_router)
app.include_router(auth_router)
app.include_router(team_router)
app.include_router(knowledge_router)
app.include_router(integration_router)
app.include_router(crm_webhook_router)
app.include_router(appointment_router)
app.include_router(calendar_router)
app.include_router(calendar_webhook_router)
app.include_router(billing_router)
app.include_router(analytics_router)
app.include_router(api_router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/health/ready")
async def readiness():
    """Readiness probe: the process is up AND the database answers.

    Distinct from /health (liveness): a load balancer routes traffic only to
    nodes whose /health/ready returns 200, so a node that lost its database
    stops receiving work instead of failing every request.
    """
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - any DB failure means "not ready"
        log.error("readiness.database_unreachable", error=str(exc))
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "database": "unreachable"},
        )
    return {"status": "ready", "database": "ok"}


def _mount_dashboard_if_built(app: FastAPI, dist_dir: str | None = None) -> None:
    """Serve the built dashboard when it is present in the image.

    The React build is a separate stage in the Dockerfile. When it exists
    (production image) its static assets are mounted and any non-API path falls
    back to index.html so client-side routes (e.g. /agent) survive a refresh.
    In dev/test the dist directory does not exist and the app stays API-only.
    """
    if dist_dir is None:
        dist_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "dashboard", "dist")
        )
    index_file = os.path.join(dist_dir, "index.html")
    if not os.path.isfile(index_file):
        return

    assets_dir = os.path.join(dist_dir, "assets")
    if os.path.isdir(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa_fallback(full_path: str):
        # Never let the SPA shell swallow unknown API/telephony/auth paths -- a
        # typo'd client call must 404 (JSON), not receive an HTML 200.
        if full_path.startswith(("api/", "auth/", "telephony/", "channels/", "health")):
            return JSONResponse(status_code=404, content={"detail": "Not found"})
        # API/telephony/auth paths are handled by the routers above; anything
        # else maps to a real file when one exists, otherwise the SPA shell.
        candidate = os.path.normpath(os.path.join(dist_dir, full_path))
        if (
            full_path
            and os.path.isfile(candidate)
            and os.path.abspath(candidate).startswith(os.path.abspath(dist_dir))
        ):
            return FileResponse(candidate)
        return FileResponse(index_file)


_mount_dashboard_if_built(app)