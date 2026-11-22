import re
from datetime import date, datetime, time
from uuid import uuid4

from pydantic import ValidationError

from assistant.bookings import (
    DUBAI,
    HOURS_TEXT,
    available_slots,
    dubai_now,
    next_open_day,
    validate_start,
)
from assistant.interpreter import Interpreter
from assistant.inventory import Inventory, canonical, normalize, phrase
from assistant.models import (
    BookingReceipt,
    ChatResponse,
    Filters,
    Listing,
    PendingBooking,
    Plan,
    Profile,
    SessionRequest,
    SessionResponse,
    SessionState,
    SlotResponse,
)
from assistant.storage import SlotTaken, Snapshot, Storage

DECLINE = (
    "I can help with the dubizzle car inventory, buying or selling questions, and viewing slots."
)
HELP = (
    "Try asking for a make, model, budget in AED, or a feature. You can ask about a result, "
    "save a favorite, or book a viewing for a dubizzle-managed listing."
)
CONFIRM = {
    "confirm",
    "confirm booking",
    "confirm the booking",
    "yes confirm",
    "yes confirm booking",
}
CANCEL = {"cancel", "cancel booking", "cancel the booking", "never mind", "nevermind"}
SKIP = {"skip", "skip qualification", "skip budget", "skip the budget", "prefer not to say"}
ORDINALS = ("first", "second", "third", "fourth", "fifth", "sixth")
LISTING_ID = re.compile(r"\b[a-f0-9]{12}\b", re.I)
PRIOR_SESSION = re.compile(
    r"\b(?:(?:last|previous|prior) (?:session|chat|conversation)|last time)\b"
)


def _merge(base: Filters, patch: Filters | None) -> Filters:
    return Filters.model_validate(
        {
            **base.model_dump(exclude_none=True),
            **(patch.model_dump(exclude_unset=True) if patch else {}),
        }
    )


def _money(value: int, currency: str = "AED") -> str:
    return f"{currency} {value:,}"


def _budget(filters: Filters) -> str | None:
    low, high = filters.min_price, filters.max_price
    currency = (filters.currency or "AED").upper()
    if low is not None and high is not None:
        return f"{_money(low, currency)} to {_money(high, currency)}"
    if high is not None:
        return f"up to {_money(high, currency)}"
    if low is not None:
        return f"from {_money(low, currency)}"
    return None


def _criteria(filters: Filters) -> str:
    criteria = [
        value
        for value in (filters.make, filters.model, filters.trim, filters.color, filters.body)
        if value
    ]
    if filters.min_year == filters.max_year and filters.min_year is not None:
        criteria.append(str(filters.min_year))
    else:
        if filters.min_year is not None:
            criteria.append(f"year {filters.min_year} or newer")
        if filters.max_year is not None:
            criteria.append(f"year {filters.max_year} or older")
    if budget := _budget(filters):
        criteria.append(budget)
    if filters.max_mileage_km is not None:
        criteria.append(f"up to {filters.max_mileage_km:,} km")
    if filters.managed is True:
        criteria.append("dubizzle-managed")
    criteria.extend(filters.features or [])
    return ", ".join(criteria)


def _facts(listing: Listing, fields: list[str], matched_features: list[str]) -> str:
    facts = {
        "price": f"Price: {_money(listing.price_aed)}."
        if listing.price_aed is not None
        else "The listing does not state a verified cash price.",
        "mileage": f"Mileage: {listing.mileage_km:,} km."
        if listing.mileage_km is not None
        else "The listing does not state mileage.",
        "warranty": f"Warranty wording from the listing: “{listing.warranty}”."
        if listing.warranty
        else "The listing does not mention a warranty.",
        "color": f"Exterior color: {listing.color}."
        if listing.color
        else "The listing does not state an exterior color.",
        "features": f"Listed features: {', '.join(matched_features)}."
        if matched_features
        else "I couldn't verify those features in the listing.",
    }
    return " ".join(facts[field] for field in fields)


class AssistantService:
    def __init__(self, inventory: Inventory, storage: Storage, interpreter: Interpreter):
        self.inventory = inventory
        self.storage = storage
        self.interpreter = interpreter

    def start(self, request: SessionRequest) -> SessionResponse:
        session_id, returning, profile = self.storage.create_session(
            request.user_id.lower(), request.name
        )
        greeting = "Welcome back" if returning else "Welcome"
        welcome = f"{greeting}{', ' + profile.name if profile.name else ''}."
        remembered = self._memory(profile, include_inquiry=False)
        welcome += f" {remembered}" if returning and remembered else f" {HELP}"
        self.storage.set_welcome(session_id, welcome)
        return SessionResponse(
            session_id=session_id, returning=returning, welcome=welcome, profile=profile
        )

    def session(self, session_id: str) -> SessionResponse:
        snapshot, returning, welcome = self.storage.load(session_id, history_limit=100)
        return SessionResponse(
            session_id=session_id,
            returning=returning,
            welcome=welcome,
            profile=snapshot.profile,
            history=snapshot.history,
        )

    def slots(self, listing: Listing, day: date, now: datetime) -> SlotResponse:
        taken = self.storage.taken_slots(listing.id, day) if listing.managed else set()
        return (
            available_slots(day, taken, now)
            if listing.managed
            else SlotResponse(date=day.isoformat(), slots=[])
        )

    def chat(self, session_id: str, message: str) -> ChatResponse:
        snapshot, _, _ = self.storage.load(session_id)
        now = dubai_now()
        state = snapshot.state
        previous_pending = state.pending_booking
        vehicle_inquiry = False
        command = normalize(message)
        # Only a separate user confirmation can commit a proposed viewing.
        if command in CONFIRM:
            if not previous_pending and snapshot.history and snapshot.history[-1].response.booking:
                receipt = snapshot.history[-1].response
                receipt.lead_csv_pending = not self.storage.refresh_csv()
                return receipt
            response = self._confirm(snapshot, now)
        elif command in CANCEL and state.pending_booking:
            state.pending_booking = None
            response = ChatResponse(reply="I cleared the pending viewing request.")
        elif command in SKIP and state.pending_booking:
            state.pending_booking.qualification_skipped = True
            response = self._booking(
                snapshot,
                Plan(action="book", references=[state.pending_booking.listing_id]),
                message,
                now,
            )
        elif command in SKIP and state.qualifying:
            state.qualifying = False
            response = ChatResponse(reply="I stopped collecting lead details.")
        else:
            plan, source = self.interpreter.interpret(message, snapshot, now)
            try:
                response = self._dispatch(plan, snapshot, session_id, message, now)
            except ValidationError:
                response = ChatResponse(
                    reply="Please check the bounds in your request and try again."
                )
            response.interpreter = source
            vehicle_inquiry = bool(response.listings) and plan.action in {
                "search",
                "details",
                "compare",
            }
            if plan.action in {"search", "details", "compare", "remember", "availability", "book"}:
                recent = snapshot.profile.recent_inquiries
                snapshot.profile.recent_inquiries = [*recent, message[:300]][-8:]

        lead_listing_id = response.booking.listing_id if response.booking else state.focus_id
        try:
            self.storage.save_turn(
                session_id,
                snapshot,
                message,
                response,
                lead_listing_id,
                vehicle_inquiry=vehicle_inquiry,
            )
        except SlotTaken:
            assert response.booking is not None
            state.pending_booking = PendingBooking(
                listing_id=response.booking.listing_id,
                date=response.booking.starts_at[:10],
                qualification_skipped=bool(
                    previous_pending and previous_pending.qualification_skipped
                ),
            )
            response = ChatResponse(reply="That slot was just taken. Please choose another time.")
            self.storage.save_turn(session_id, snapshot, message, response)
        if response.lead_saved:
            response.lead_csv_pending = not self.storage.refresh_csv()
        return response

    def _dispatch(
        self, plan: Plan, snapshot: Snapshot, session_id: str, message: str, now: datetime
    ) -> ChatResponse:
        state, profile = snapshot.state, snapshot.profile
        match plan.action:
            case "search":
                return self._search(plan, snapshot)
            case "details" | "compare":
                return self._details(plan, state, message)
            case "recall_vehicle":
                return self._recall_vehicle(snapshot, session_id, message)
            case "recall":
                reply = (
                    self._memory(profile, include_inquiry=True)
                    or "I haven't saved any preferences yet."
                )
                return ChatResponse(reply=reply, listings=self._listings(profile.saved_ids[-6:]))
            case "remember":
                return self._remember(plan, snapshot, message)
            case "availability":
                return self._availability(plan, state, message, now)
            case "book":
                return self._booking(snapshot, plan, message, now)
            case "qualify":
                if state.pending_booking:
                    return self._booking(snapshot, plan, message, now)
                self._apply_preferences(profile, plan)
                state.qualifying = True
                if question := self._qualification_question(profile):
                    return ChatResponse(reply=question)
                state.qualifying = False
                return ChatResponse(
                    reply="I recorded your buying requirements as a lead.", lead_saved=True
                )
            case "greet":
                return ChatResponse(
                    reply=f"Hi{', ' + profile.name if profile.name else ''}! {HELP}"
                )
            case "thanks":
                return ChatResponse(
                    reply="You're welcome. I'm here if you want to explore another car."
                )
            case "help":
                return ChatResponse(reply=HELP)
            case "clarify":
                return ChatResponse(reply="Which car or requirement should I use to help you?")
            case _:
                return ChatResponse(reply=DECLINE)

    def _recall_vehicle(self, snapshot: Snapshot, session_id: str, message: str) -> ChatResponse:
        previous = self.storage.latest_vehicle_inquiry(
            snapshot.user_id,
            session_id,
            prior_sessions_only=bool(PRIOR_SESSION.search(normalize(message))),
        )
        if previous is None:
            return ChatResponse(reply="I don't have an earlier car inquiry for this user ID yet.")
        exchange, current_session = previous
        discussed = exchange.response.listings
        opening = "You" if current_session else "In a previous session, you"
        if len(discussed) == 1:
            reply = f"{opening} asked about the {discussed[0].label}."
        else:
            cars = ", ".join(dict.fromkeys(car.label for car in discussed))
            inquiry = exchange.message.rstrip(".!?")[:300]
            reply = f"{opening} asked “{inquiry}”. I showed these listings: {cars}."
        listings = self._listings([car.id for car in discussed])
        snapshot.state.result_ids = [car.id for car in listings]
        snapshot.state.focus_id = listings[0].id if len(discussed) == len(listings) == 1 else None
        return ChatResponse(reply=reply, listings=listings)

    def _search(self, plan: Plan, snapshot: Snapshot) -> ChatResponse:
        state = snapshot.state
        starting = {
            "new": Filters(),
            "refine": state.filters,
            "saved": snapshot.profile.preferences,
        }[plan.search_mode]
        filters = _merge(starting, plan.filters)
        criteria = _criteria(filters)
        if plan.search_mode == "saved" and not criteria:
            return ChatResponse(
                reply="I don't have saved search preferences yet. What are you looking for?"
            )
        state.filters = filters
        if criteria:
            snapshot.profile.preferences = filters
        if plan.needs:
            snapshot.profile.needs = plan.needs
        try:
            result = self.inventory.search(filters)
        except ValueError as error:
            state.result_ids, state.focus_id = [], None
            return ChatResponse(reply=f"{error} I saved your stated budget for later.")
        state.result_ids = [listing.id for listing in result.listings]
        state.focus_id = state.result_ids[0] if result.total == 1 else None
        suffix = f" for {criteria}" if criteria else ""
        if not result.total:
            price_note = (
                " Price filters only include listings with a verifiable cash price."
                if _budget(filters)
                else ""
            )
            return ChatResponse(
                reply=f"I found no matching listings{suffix}.{price_note} Want to adjust a filter?"
            )
        if result.total > len(result.listings):
            reply = f"I found {result.total} matching cars{suffix}. Here are the first {len(result.listings)}."
        else:
            noun = "listing" if result.total == 1 else "listings"
            reply = f"I found {result.total} matching {noun}{suffix}."
        return ChatResponse(reply=reply, listings=result.listings)

    def _listings(self, ids: list[str]) -> list[Listing]:
        return [listing for identifier in ids if (listing := self.inventory.get(identifier))]

    def _resolve(self, references: list[str], state: SessionState, message: str) -> list[Listing]:
        text = normalize(message)
        results = self._listings(state.result_ids)
        mentioned_makes = [make for make in self.inventory.makes if phrase(text, normalize(make))]
        ordinals = [
            index
            for index, name in enumerate(ORDINALS)
            if phrase(text, name) or re.search(rf"#\s*{index + 1}\b", message)
        ]
        if ordinals:
            numbered = (
                [car for car in results if car.make in mentioned_makes]
                if mentioned_makes
                else results
            )
            return [numbered[index] for index in ordinals if index < len(numbered)]
        named = [
            car
            for car in results
            if phrase(text, normalize(car.model))
            and (not mentioned_makes or car.make in mentioned_makes)
        ]
        explicit_ids = LISTING_ID.findall(message.lower())
        if explicit_ids:
            return self._listings(list(dict.fromkeys(explicit_ids)))
        if named:
            return named
        explicit = self._listings(list(dict.fromkeys(references)))
        if explicit:
            return explicit
        local_filters = self.interpreter.local_filters(message)
        if local_filters.model or local_filters.make or mentioned_makes:
            matching_results = [
                car
                for car in results
                if not local_filters.make or canonical(car.make) == canonical(local_filters.make)
            ]
            if local_filters.model:
                matching_results = [
                    car
                    for car in matching_results
                    if canonical(car.model) == canonical(local_filters.model)
                ]
            if matching_results:
                return matching_results
            return self.inventory.search(
                Filters(
                    make=local_filters.make, model=local_filters.model, trim=local_filters.trim
                ),
                limit=4,
            ).listings
        focused = self.inventory.get(state.focus_id)
        return [focused] if focused else results[:1] if len(results) == 1 else []

    def _details(self, plan: Plan, state: SessionState, message: str) -> ChatResponse:
        cars = self._resolve(plan.references, state, message)
        comparing = plan.action == "compare"
        if comparing and not cars and len(state.result_ids) == 2:
            cars = self._listings(state.result_ids)
        if comparing and len(cars) not in {2, 3, 4}:
            return ChatResponse(
                reply="Which listings should I compare? You can say “compare the first and second.”"
            )
        if not comparing and len(cars) != 1:
            return ChatResponse(
                reply="Which listing do you mean? Give me its result number, model, or listing ID."
            )
        if not comparing:
            state.focus_id = cars[0].id
        fields = [field for field in plan.fields if field != "overview"] or [
            "price",
            "mileage",
            "color",
            "warranty",
        ]
        features = (plan.filters.features or []) if plan.filters else []
        if "features" in fields and not features:
            features = [
                "GCC specs",
                "cruise control",
                "sunroof",
                "Apple CarPlay",
                "parking sensors",
                "reversing camera",
                "360 degree camera",
                "leather seats",
            ]
        parts = [
            f"{car.label}. {_facts(car, fields, self.inventory.matching_features(car.id, features))}"
            for car in cars
        ]
        return ChatResponse(reply="\n\n".join(parts), listings=cars)

    def _remember(self, plan: Plan, snapshot: Snapshot, message: str) -> ChatResponse:
        profile = snapshot.profile
        self._apply_preferences(profile, plan)
        criteria = _criteria(plan.filters) if plan.filters else ""
        saving_car = bool(
            re.search(r"\b(?:favorite|favourite|like|love|save|remember)\b", normalize(message))
        )
        text = normalize(message)
        names_listing = any(phrase(text, ordinal) for ordinal in ORDINALS) or phrase(
            text, "this car"
        )
        if saving_car and (plan.references or not (criteria or plan.needs or plan.name)):
            cars = self._resolve(plan.references, snapshot.state, message)
            if len(cars) == 1 and (not criteria or names_listing):
                car = cars[0]
                profile.saved_ids = [
                    *([item for item in profile.saved_ids if item != car.id]),
                    car.id,
                ][-10:]
                snapshot.state.focus_id = car.id
                return ChatResponse(
                    reply=f"I saved the {car.label} to your favorites.", listings=cars
                )
        if plan.name:
            return ChatResponse(reply=f"I'll remember your name, {profile.name}.")
        if criteria or plan.needs:
            return ChatResponse(
                reply=f"I saved your preferences for next time. {self._memory(profile)}"
            )
        return ChatResponse(
            reply="What preferences should I remember, or which listing should I save?"
        )

    def _memory(self, profile: Profile, *, include_inquiry: bool = False) -> str:
        parts = []
        if criteria := _criteria(profile.preferences):
            parts.append(f"Your saved search preferences are {criteria}.")
        if profile.needs:
            parts.append(f"You said you need {profile.needs.rstrip('.')}.")
        if cars := self._listings(profile.saved_ids[-3:]):
            parts.append(f"Saved cars include {', '.join(car.label for car in cars)}.")
        if include_inquiry and profile.recent_inquiries:
            inquiry = profile.recent_inquiries[-1].rstrip(".")
            parts.append(f"Your latest inventory inquiry was “{inquiry}”.")
        return " ".join(parts)

    @staticmethod
    def _apply_preferences(profile: Profile, plan: Plan) -> None:
        if plan.filters:
            profile.preferences = _merge(profile.preferences, plan.filters)
        if plan.needs:
            profile.needs = plan.needs
        if plan.name:
            profile.name = plan.name

    def _availability(
        self, plan: Plan, state: SessionState, message: str, now: datetime
    ) -> ChatResponse:
        cars = self._resolve(plan.references, state, message)
        if not cars:
            return ChatResponse(reply=f"{HOURS_TEXT} Which car should I check?")
        if len(cars) != 1:
            return ChatResponse(reply="Which listing should I check for availability?")
        car = cars[0]
        if not car.managed:
            return ChatResponse(
                reply=f"Online viewing slots are available for dubizzle-managed listings. "
                f"I can show you managed alternatives to the {car.label}."
            )
        try:
            day = date.fromisoformat(plan.date) if plan.date else next_open_day(now)
        except ValueError:
            return ChatResponse(reply="Which date should I check? Use YYYY-MM-DD or a weekday.")
        state.focus_id = car.id
        return self._slot_message(car, day, now)

    def _slot_message(self, car: Listing, day: date, now: datetime) -> ChatResponse:
        slots = self.slots(car, day, now)
        if slots.slots:
            reply = f"Available starts for the {car.label} on {day:%A, %d %B} (Dubai time): {', '.join(slots.slots)}."
        else:
            reply = f"There are no available starts for the {car.label} on {day:%A, %d %B}. {HOURS_TEXT}"
        return ChatResponse(reply=reply, slots=slots)

    def _booking(self, snapshot: Snapshot, plan: Plan, message: str, now: datetime) -> ChatResponse:
        state, profile = snapshot.state, snapshot.profile
        pending = state.pending_booking
        if pending and plan.action == "qualify":
            cars = self._listings([pending.listing_id])
        else:
            cars = self._resolve(plan.references, state, message)
            if not cars and pending and not LISTING_ID.search(message):
                cars = self._listings([pending.listing_id])
        if len(cars) != 1:
            return ChatResponse(
                reply="Which listing would you like to view? Give me its result number or ID."
            )
        car = cars[0]
        if not car.managed:
            return ChatResponse(
                reply=f"The {car.label} has no dubizzle-managed viewing slots. "
                "I can help you find a managed listing."
            )
        self._apply_preferences(profile, plan)
        if pending is None or pending.listing_id != car.id:
            pending = PendingBooking(listing_id=car.id)
        state.pending_booking, state.focus_id = pending, car.id
        if plan.date:
            pending.date = plan.date
        if plan.time:
            pending.time = plan.time
        pending.starts_at = None
        if not pending.date:
            reply = self._slot_message(car, next_open_day(now), now)
            reply.reply = f"What date would you like to view the {car.label}?\n\n{reply.reply}"
            return reply
        try:
            day = date.fromisoformat(pending.date)
            hour = (
                time.fromisoformat(pending.time)
                if pending.time and re.fullmatch(r"\d{2}:\d{2}", pending.time)
                else None
            )
        except ValueError:
            pending.date, pending.time = None, None
            return ChatResponse(
                reply="Please give me a valid date and a time such as 10:00 or 2 pm."
            )
        if hour is None:
            return self._slot_message(car, day, now)
        starts = datetime.combine(day, hour, DUBAI)
        if error := validate_start(starts, now):
            pending.time = None
            return ChatResponse(reply=error)
        if starts.isoformat() in self.storage.taken_slots(car.id, day):
            pending.time = None
            reply = self._slot_message(car, day, now)
            reply.reply = f"That slot is already booked. {reply.reply}"
            return reply
        pending.starts_at = starts.isoformat()
        # A proposal lives in the session until confirmation wins the database slot.
        if not pending.qualification_skipped and (
            question := self._qualification_question(profile)
        ):
            return ChatResponse(reply=f"I found that slot. {question} You can also say “skip.”")
        return ChatResponse(
            reply=f"Proposed viewing for the {car.label} on {starts:%A, %d %B at %H:%M} Dubai time. "
            "Reply “confirm booking” to reserve it or “cancel” to clear the request."
        )

    @staticmethod
    def _qualification_question(profile: Profile) -> str | None:
        if not _budget(profile.preferences):
            return "What is your budget range or maximum budget? For example, budget AED 90,000."
        preferences = profile.preferences
        if not (
            profile.needs
            or preferences.body
            or preferences.color
            or preferences.features
            or preferences.max_mileage_km is not None
            or preferences.min_year is not None
        ):
            return "What else do you need from the car? For example, “I need room for a stroller.”"
        return None

    def _confirm(self, snapshot: Snapshot, now: datetime) -> ChatResponse:
        pending = snapshot.state.pending_booking
        if not pending or not pending.starts_at:
            return ChatResponse(
                reply="Please select a managed listing and an available date and time first."
            )
        if not pending.qualification_skipped and (
            question := self._qualification_question(snapshot.profile)
        ):
            return ChatResponse(reply=f"{question} You can also say “skip.”")
        car = self.inventory.get(pending.listing_id)
        if car is None or not car.managed:
            snapshot.state.pending_booking = None
            return ChatResponse(
                reply="That listing is no longer bookable. Please choose another managed car."
            )
        starts = datetime.fromisoformat(pending.starts_at)
        if error := validate_start(starts, now):
            pending.starts_at = pending.time = None
            return ChatResponse(reply=error)
        receipt = BookingReceipt(
            id=uuid4().hex[:12], listing_id=car.id, starts_at=starts.isoformat()
        )
        snapshot.state.pending_booking = None
        return ChatResponse(
            reply=f"Viewing booked for the {car.label} on {starts:%A, %d %B at %H:%M} Dubai time. "
            f"Your reference is {receipt.id}.",
            booking=receipt,
            lead_saved=True,
        )
