from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Action = Literal[
    "search",
    "details",
    "compare",
    "recall",
    "recall_vehicle",
    "remember",
    "availability",
    "book",
    "qualify",
    "greet",
    "thanks",
    "help",
    "decline",
    "clarify",
]
Detail = Literal["overview", "price", "mileage", "warranty", "color", "features"]


class Filters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    make: ShortText | None = None
    model: ShortText | None = None
    trim: ShortText | None = None
    body: ShortText | None = None
    color: ShortText | None = None
    features: list[ShortText] | None = Field(default=None, max_length=6)
    min_year: int | None = Field(default=None, ge=1886, le=2100)
    max_year: int | None = Field(default=None, ge=1886, le=2100)
    min_price: int | None = Field(default=None, ge=0, le=100_000_000)
    max_price: int | None = Field(default=None, ge=0, le=100_000_000)
    currency: ShortText | None = None
    max_mileage_km: int | None = Field(default=None, ge=0, le=2_000_000)
    managed: bool | None = None
    sort: Literal["relevance", "price_asc", "year_desc", "mileage_asc"] | None = None

    @model_validator(mode="after")
    def valid_ranges(self) -> "Filters":
        for low, high in ((self.min_price, self.max_price), (self.min_year, self.max_year)):
            if low is not None and high is not None and low > high:
                raise ValueError("The lower bound cannot exceed the upper bound")
        return self


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Action
    filters: Filters | None = None
    search_mode: Literal["new", "refine", "saved"] = "new"
    references: list[ShortText] = Field(default_factory=list, max_length=4)
    fields: list[Detail] = Field(default_factory=list, max_length=6)
    date: ShortText | None = None
    time: ShortText | None = None
    needs: ShortText | None = None
    name: ShortText | None = None


class Listing(BaseModel):
    id: str
    make: str
    model: str
    trim: str | None = None
    year: int
    title: str
    photo_url: str | None = None
    price_aed: int | None = None
    mileage_km: int | None = None
    warranty: str | None = None
    color: str | None = None
    body: str | None = None
    managed: bool = False

    @property
    def label(self) -> str:
        return f"{self.year} {self.make} {self.model}"


class InventoryResponse(BaseModel):
    total: int
    listings: list[Listing]


class SlotResponse(BaseModel):
    date: str
    timezone: str = "Asia/Dubai"
    duration_minutes: int = 60
    slots: list[str]


class SessionRequest(BaseModel):
    user_id: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=64,
            pattern=r"^[A-Za-z0-9_.-]+$",
        ),
    ]
    name: ShortText | None = None


class Profile(BaseModel):
    name: str | None = None
    preferences: Filters = Field(default_factory=Filters)
    needs: str | None = None
    saved_ids: list[str] = Field(default_factory=list)
    recent_inquiries: list[str] = Field(default_factory=list)


class PendingBooking(BaseModel):
    listing_id: str
    date: str | None = None
    time: str | None = None
    starts_at: str | None = None
    qualification_skipped: bool = False


class SessionState(BaseModel):
    result_ids: list[str] = Field(default_factory=list)
    focus_id: str | None = None
    filters: Filters = Field(default_factory=Filters)
    pending_booking: PendingBooking | None = None
    qualifying: bool = False


class BookingReceipt(BaseModel):
    id: str
    listing_id: str
    starts_at: str


class ChatRequest(BaseModel):
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class ChatResponse(BaseModel):
    reply: str
    listings: list[Listing] = Field(default_factory=list)
    slots: SlotResponse | None = None
    booking: BookingReceipt | None = None
    interpreter: Literal["gemini", "local"] = "local"
    lead_saved: bool = False
    lead_csv_pending: bool = False


class Exchange(BaseModel):
    message: str
    response: ChatResponse


class SessionResponse(BaseModel):
    session_id: str
    returning: bool
    welcome: str
    profile: Profile
    history: list[Exchange] = Field(default_factory=list)
