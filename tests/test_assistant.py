import csv
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from pydantic import SecretStr

from assistant.bookings import DUBAI
from assistant.config import Settings
from assistant.interpreter import Interpreter
from assistant.models import Filters, Plan
from assistant.service import DECLINE
from main import create_app


@pytest.fixture
def settings(tmp_path, monkeypatch):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Inventory"
    sheet.append(["Listing_ID", "Make", "Model", "Trim", "Year", "Title", "Description"])
    sheet.append(
        [
            1,
            "Honda",
            "CR-V",
            "EX-L",
            2021,
            "Honda CR-V",
            "White exterior SUV<br>Mileage: 79,000 km<br>AED 1,048/month<br>Panoramic sunroof",
        ]
    )
    sheet.append(
        [
            2,
            "Mercedes-Benz",
            "C-Class",
            "C200",
            2021,
            "Mercedes-Benz C200 | Under Warranty",
            "Black exterior sedan<br>77,820 km<br>Price: AED 106,000<br>"
            "Buy this car with confidence from dubizzle cars!",
        ]
    )
    sheet.append(
        [
            3,
            "Honda",
            "Civic",
            "LX",
            2020,
            "Honda Civic",
            "Blue exterior sedan<br>Price: AED 51,000<br>Without sunroof<br>Service report for AED 369",
        ]
    )
    path = tmp_path / "inventory.xlsx"
    workbook.save(path)
    monkeypatch.setattr(
        "assistant.service.dubai_now", lambda: datetime(2026, 11, 20, 12, tzinfo=DUBAI)
    )
    monkeypatch.setattr("main.dubai_now", lambda: datetime(2026, 11, 20, 12, tzinfo=DUBAI))
    return Settings(
        _env_file=None,
        inventory_path=path,
        inventory_sheet=sheet.title,
        runtime_dir=tmp_path / "runtime",
        gemini_api_key=SecretStr(""),
    )


def _start(client, user_id, name=None):
    response = client.post("/sessions", json={"user_id": user_id, "name": name})
    assert response.status_code == 201
    return response.json()


def _chat(client, session, message):
    response = client.post(f"/sessions/{session['session_id']}/chat", json={"message": message})
    assert response.status_code == 200, response.text
    return response.json()


def test_inventory_and_context(settings):
    with TestClient(create_app(settings)) as client:
        response = client.get("/inventory", params={"max_price": 110000, "sort": "price_asc"})
        assert [c["price_aed"] for c in response.json()["listings"]] == [51000, 106000]
        response = client.get("/inventory", params={"features": "sunroof"})
        assert [c["model"] for c in response.json()["listings"]] == ["CR-V"]
        assert (
            client.get("/inventory", params={"min_price": 110000, "max_price": 100}).status_code
            == 422
        )
        session = _start(client, "driver", "Alex")
        search = _chat(client, session, "Show me Honda listings")
        assert [c["model"] for c in search["listings"]] == ["CR-V", "Civic"]
        assert "79,000 km" in _chat(client, session, "Mileage on the first Honda?")["reply"]
        warranty = _chat(client, session, "Is there a warranty on it?")
        assert warranty["listings"][0]["model"] == "CR-V"
        assert "does not mention a warranty" in warranty["reply"]
        compare = _chat(client, session, "Compare the first and second")
        assert [c["model"] for c in compare["listings"]] == ["CR-V", "Civic"]
        assert "favorites" in _chat(client, session, "Save that car")["reply"]
        assert not _chat(client, session, "Find white SUVs under $20k")["listings"]
        assert (
            "dubizzle car inventory"
            in _chat(client, session, "Write Python code about Honda")["reply"]
        )

    with TestClient(create_app(settings)) as client:
        new = _start(client, "DRIVER")
        assert new["returning"] and new["session_id"] != session["session_id"]
        assert new["history"] == []
        assert (
            "Alex" in new["welcome"]
            and "USD 19,999" in new["welcome"]
            and "Honda CR-V" in new["welcome"]
        )
        assert new["profile"]["preferences"]["make"] is None
        old = client.get(f"/sessions/{session['session_id']}").json()
        assert len(old["history"]) == 7
        assert client.get("/sessions/unknown").status_code == 404


def test_vehicle_recall_in_same_and_previous_sessions(settings):
    question = "what car did i ask about earlier?"
    with TestClient(create_app(settings)) as client:
        session = _start(client, "driver")
        assert "earlier car inquiry" in _chat(client, session, question)["reply"]
        _chat(client, session, "Show me Honda listings")
        results = _chat(client, session, question)
        assert "I showed these listings" in results["reply"]
        assert [car["model"] for car in results["listings"]] == ["CR-V", "Civic"]
        _chat(client, session, "Mileage on the first Honda?")
        assert _chat(client, session, question)["reply"] == "You asked about the 2021 Honda CR-V."
        _chat(client, session, "Show me Honda SUVs under AED 20")

        # Simulate the exchange format that was already on disk before vehicle recall shipped.
        storage = client.app.state.assistant.storage
        with storage.connection() as conn:
            conn.execute(
                "UPDATE exchanges SET response = json_remove(response, '$.vehicle_inquiry')"
            )

    with TestClient(create_app(settings)) as client:
        new_session = _start(client, "DRIVER")
        assert new_session["returning"] and not new_session["history"]
        recalled = _chat(client, new_session, question)
        assert recalled["reply"] == "In a previous session, you asked about the 2021 Honda CR-V."
        assert recalled["listings"][0]["model"] == "CR-V"
        assert "79,000 km" in _chat(client, new_session, "What is the mileage on it?")["reply"]

        _chat(client, new_session, "Show me Mercedes-Benz listings")
        assert "Mercedes-Benz C-Class" in _chat(client, new_session, question)["reply"]
        assert (
            "Honda CR-V"
            in _chat(client, new_session, "Which car did I ask about last session?")["reply"]
        )
        assert "Mercedes-Benz C-Class" in _chat(client, new_session, question)["reply"]
        assert (
            "earlier car inquiry"
            in _chat(client, _start(client, "other-driver"), question)["reply"]
        )


def test_explicit_budget_applies_exact_boundary_to_gemini_plan(settings, monkeypatch):
    settings = settings.model_copy(update={"gemini_api_key": SecretStr("test-key")})
    monkeypatch.setattr(
        Interpreter,
        "_gemini",
        lambda *_: Plan(action="search", filters=Filters(currency="AED", max_price=106000)),
    )
    with TestClient(create_app(settings)) as client:
        session = _start(client, "driver")
        strict = _chat(client, session, "Find cars under AED 106,000")
        assert strict["interpreter"] == "gemini"
        assert [car["price_aed"] for car in strict["listings"]] == [51000]
        inclusive = _chat(client, session, "Find cars up to 106,000 AED")
        assert {car["price_aed"] for car in inclusive["listings"]} == {51000, 106000}
        fractional = _chat(client, session, "Find cars below AED 106,000.50")
        assert {car["price_aed"] for car in fractional["listings"]} == {51000, 106000}
        dollars = _chat(client, session, "Find cars under $20k")
        assert not dollars["listings"] and "provide a budget in AED" in dollars["reply"]


def test_greeting_and_scope_guardrails(settings):
    with TestClient(create_app(settings)) as client:
        session = _start(client, "driver", "Alex")
        assert _chat(client, session, "Hello")["reply"].startswith("Hi, Alex!")
        for question in (
            "Who was the first president of the United States?",
            "Write Python code about Honda.",
            "Compare these listings with AutoTrader.",
        ):
            response = _chat(client, session, question)
            assert response["reply"] == DECLINE
            assert not response["listings"] and not response["booking"]
        assert not client.get(f"/sessions/{session['session_id']}").json()["profile"][
            "recent_inquiries"
        ]


def test_bookings_conflict_and_lead_export(settings):
    with TestClient(create_app(settings)) as client:
        session = _start(client, "buyer", "Alex")
        other = _start(client, "other")
        car = client.get("/inventory", params={"managed": "true"}).json()["listings"][0]
        car_id = car["id"]
        slots = f"/inventory/{car_id}/slots"
        assert client.get(slots, params={"date": "2026-11-22"}).json()["slots"] == []
        assert client.get(slots, params={"date": "2026-11-20"}).json()["slots"][0] == "13:00"
        assert client.get(slots, params={"date": "2026-11-21"}).json()["slots"][-1] == "19:00"
        assert (
            "no dubizzle-managed"
            in _chat(client, session, "Book the Honda CR-V tomorrow at 10 am")["reply"]
        )
        assert not _chat(client, session, "confirm booking")["booking"]
        assert (
            "closed on Sundays"
            in _chat(client, session, f"Book {car_id} on Sunday at 10 am")["reply"]
        )
        assert "budget" in _chat(client, session, f"Book {car_id} on Saturday at 10 am")["reply"]
        assert "What else" in _chat(client, session, "My budget is AED 110,000.")["reply"]
        assert "confirm booking" in _chat(client, session, "I need room for a stroller")["reply"]
        _chat(client, other, f"Book {car_id} on Saturday at 10 am")
        _chat(client, other, "skip")
        confirmed = _chat(client, session, "confirm booking")
        assert confirmed["lead_saved"] and not confirmed["lead_csv_pending"]
        assert confirmed["booking"]["listing_id"] == car_id
        assert "just taken" in _chat(client, other, "confirm booking")["reply"]
        assert _chat(client, session, "confirm booking")["booking"] == confirmed["booking"]
        assert "10:00" not in client.get(slots, params={"date": "2026-11-21"}).json()["slots"]
    with (settings.runtime_dir / "leads.csv").open(newline="", encoding="utf-8") as handle:
        records = list(csv.DictReader(handle))
    assert len(records) == 1
    assert records[0]["budget_max"] == "110000"
    assert records[0]["needs"] == "room for a stroller"
    assert records[0]["booking_id"] == confirmed["booking"]["id"]
