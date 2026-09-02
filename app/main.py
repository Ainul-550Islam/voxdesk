from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)   # use Alembic in production

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


app = FastAPI(title="VoxDesk", version="0.4.0", lifespan=lifespan)

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

