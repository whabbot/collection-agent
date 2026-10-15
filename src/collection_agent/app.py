"""FastAPI application factory."""

from fastapi import FastAPI, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from collection_agent.deps import SessionDep, get_session
from collection_agent.email_api import router as email_router
from collection_agent.inbound_api import router as inbound_router
from collection_agent.invoices_api import router as invoices_router
from collection_agent.policy_api import router as policy_router
from collection_agent.webhooks_api import router as webhooks_router

__all__ = ["app", "create_app", "get_session"]


def create_app() -> FastAPI:
    app = FastAPI(title="Collection agent")

    @app.get("/healthz")
    def healthz(session: SessionDep, response: Response) -> dict[str, str]:
        try:
            session.execute(text("SELECT 1"))
        except SQLAlchemyError:
            response.status_code = 503
            return {"status": "db_unavailable"}
        return {"status": "ok"}

    app.include_router(invoices_router)
    app.include_router(webhooks_router)
    app.include_router(email_router)
    app.include_router(inbound_router)
    app.include_router(policy_router)
    return app


app = create_app()
