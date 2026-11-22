from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import Field

from assistant.bookings import dubai_now
from assistant.config import Settings
from assistant.interpreter import Interpreter
from assistant.inventory import Inventory
from assistant.models import (
    ChatRequest,
    ChatResponse,
    Filters,
    InventoryResponse,
    Listing,
    SessionRequest,
    SessionResponse,
    SlotResponse,
)
from assistant.service import AssistantService
from assistant.storage import ConcurrentTurn, SessionMissing, Storage


class InventoryQuery(Filters):
    limit: int = Field(default=12, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


def get_service(request: Request) -> AssistantService:
    return request.app.state.assistant


Service = Annotated[AssistantService, Depends(get_service)]


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(api: FastAPI) -> AsyncIterator[None]:
        config = settings if settings is not None else Settings()
        # Load the catalog once and share a pooled Gemini client for the life of the API.
        inventory = Inventory.load(config.inventory_path, config.inventory_sheet)
        storage = Storage(config.runtime_dir)
        with httpx.Client(timeout=httpx.Timeout(12.0, connect=4.0)) as client:
            interpreter = Interpreter(
                inventory,
                client,
                config.gemini_api_key.get_secret_value(),
                config.gemini_model,
            )
            api.state.assistant = AssistantService(inventory, storage, interpreter)
            yield

    api = FastAPI(title="dubizzle Car Assistant", version="1.0.0", lifespan=lifespan)

    @api.get("/health")
    def health(service: Service) -> dict[str, str | int]:
        return {"status": "ok", "listings": len(service.inventory.records)}

    @api.post("/sessions", status_code=201)
    def start_session(body: SessionRequest, service: Service) -> SessionResponse:
        return service.start(body)

    @api.get("/sessions/{session_id}")
    def get_session(session_id: str, service: Service) -> SessionResponse:
        try:
            return service.session(session_id)
        except SessionMissing as error:
            raise HTTPException(404, "Session not found") from error

    @api.post("/sessions/{session_id}/chat")
    def chat(session_id: str, body: ChatRequest, service: Service) -> ChatResponse:
        try:
            return service.chat(session_id, body.message)
        except SessionMissing as error:
            raise HTTPException(404, "Session not found") from error
        except ConcurrentTurn as error:
            raise HTTPException(409, "Another turn updated your session. Please retry.") from error

    @api.get("/inventory")
    def search_inventory(
        query: Annotated[InventoryQuery, Query()], service: Service
    ) -> InventoryResponse:
        filters = Filters.model_validate(query.model_dump(exclude={"limit", "offset"}))
        try:
            return service.inventory.search(filters, query.limit, query.offset)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @api.get("/inventory/{listing_id}")
    def get_listing(listing_id: str, service: Service) -> Listing:
        if listing := service.inventory.get(listing_id):
            return listing
        raise HTTPException(404, "Listing not found")

    @api.get("/inventory/{listing_id}/slots")
    def get_slots(
        listing_id: str, day: Annotated[date, Query(alias="date")], service: Service
    ) -> SlotResponse:
        if listing := service.inventory.get(listing_id):
            return service.slots(listing, day, dubai_now())
        raise HTTPException(404, "Listing not found")

    return api


app = create_app()
