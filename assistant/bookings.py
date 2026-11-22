from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from assistant.models import SlotResponse

DUBAI = ZoneInfo("Asia/Dubai")
OPEN_HOUR = 8
CLOSE_HOUR = 20
HOURS_TEXT = "Monday to Saturday, 8 am to 8 pm Dubai time. Each viewing lasts one hour."


def dubai_now() -> datetime:
    return datetime.now(DUBAI)


def available_slots(day: date, taken: set[str], now: datetime) -> SlotResponse:
    starts = (
        datetime.combine(day, time(hour), DUBAI)
        for hour in range(OPEN_HOUR, CLOSE_HOUR)
        if day.weekday() < 6 and day >= now.date()
    )
    return SlotResponse(
        date=day.isoformat(),
        slots=[
            start.strftime("%H:%M")
            for start in starts
            if start > now and start.isoformat() not in taken
        ],
    )


def next_open_day(now: datetime) -> date:
    day = now.date()
    while not available_slots(day, set(), now).slots:
        day += timedelta(days=1)
    return day


def validate_start(value: datetime, now: datetime) -> str | None:
    if value <= now:
        return "That time has passed. Please choose a future slot."
    if value.weekday() == 6:
        return "Viewings are closed on Sundays. Please choose Monday to Saturday."
    if not OPEN_HOUR <= value.hour < CLOSE_HOUR or value.minute:
        return "Please choose a slot on the hour between 8 am and 7 pm Dubai time."
    return None
