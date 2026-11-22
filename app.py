from html import escape

import httpx
import streamlit as st

from assistant.config import ClientSettings
from assistant.models import ChatResponse, Exchange, Listing, SessionResponse

st.set_page_config(page_title="dubizzle cars | Inventory Assistant", page_icon="🚗")

st.markdown(
    """
    <style>
      .block-container { max-width: 58rem; padding-top: clamp(2.5rem, 5vw, 3.25rem); padding-bottom: 3rem; }
      .hero { border-radius: 16px; padding: 1.35rem clamp(1rem, 4vw, 2rem); color: #fff;
              background: linear-gradient(115deg, #24191C 35%, #521625);
              border-left: 5px solid #CB112D; margin-bottom: 1rem; }
      .hero p { margin: .45rem 0 0; color: #F5DDE2; }
      .hero h1 { padding: .35rem 0 0; margin: 0; color: #fff; font-size: clamp(1.8rem, 5vw, 2.65rem); }
      .hero strong { color: #FFB9C4; font-size: .95rem; }
      .chat-copy { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.6; margin: 0; }
    </style>
    <section class="hero" aria-label="dubizzle cars">
      <strong>dubizzle cars</strong>
      <h1>Find your next car</h1>
      <p>Explore the inventory, compare listings, and book a viewing in Dubai.</p>
    </section>
    """,
    unsafe_allow_html=True,
)

API_URL = ClientSettings().api_base_url.rstrip("/")
PROMPTS = (
    "Show me Toyota listings",
    "Find dubizzle-managed cars under AED 110,000",
    "What are my saved preferences?",
)


def api(method: str, path: str, **kwargs) -> dict:
    response = httpx.request(method, f"{API_URL}{path}", timeout=25, **kwargs)
    response.raise_for_status()
    return response.json()


def api_error(error: httpx.HTTPError) -> None:
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status == 422:
            st.error(
                "Check the form. Use letters, numbers, dots, hyphens, or underscores in your ID."
            )
        elif status == 409:
            st.error("Another turn updated this session. Please send your message again.")
        elif status == 404:
            st.error("This session no longer exists. Start a new one from the sidebar.")
        else:
            st.error(f"The API returned status {status}. Try again in a moment.")
    else:
        st.error("The local API is unreachable. Check that the FastAPI server is running.")


def chat_text(text: str) -> None:
    st.markdown(f'<p class="chat-copy">{escape(text)}</p>', unsafe_allow_html=True)


def render_listing(car: Listing, index: int) -> None:
    price = f"AED {car.price_aed:,}" if car.price_aed is not None else "Price unavailable"
    mileage = f"{car.mileage_km:,} km" if car.mileage_km is not None else None
    summary = " · ".join(value for value in (f"**{index}. {car.label}**", price, mileage) if value)
    with st.expander(summary):
        if car.photo_url:
            st.image(car.photo_url, width=250)
        specs = " · ".join(value for value in (car.trim, car.color, car.body) if value)
        if specs:
            st.text(specs)
        if mileage is None:
            st.caption("Mileage unavailable")
        st.caption("Eligible for managed viewing" if car.managed else "No managed viewing")
        st.caption("Listing ID")
        st.code(car.id, language=None)


def render_reply(response: ChatResponse) -> None:
    with st.chat_message("assistant"):
        chat_text(response.reply)
        for index, car in enumerate(response.listings, 1):
            render_listing(car, index)
        if response.booking:
            st.success("Viewing reserved in the prototype")
        if response.lead_csv_pending:
            st.warning("The lead was saved to the database. The CSV export is pending.")


def main() -> None:
    with st.sidebar:
        st.header("Your session")
        if session := st.session_state.get("session"):
            st.text(f"User ID: {st.session_state.user_id}")
            st.caption("Start a new session with the same ID to recover your saved preferences.")
            if st.button("New session", width="stretch"):
                del st.session_state.session
                st.rerun()
        else:
            with st.form("start-session"):
                user_id = st.text_input(
                    "User ID", value=st.session_state.get("user_id", ""), max_chars=64
                )
                name = st.text_input("Name (optional)", max_chars=100)
                if st.form_submit_button("Start chat", type="primary", width="stretch"):
                    try:
                        result = api(
                            "POST",
                            "/sessions",
                            json={"user_id": user_id, "name": name.strip() or None},
                        )
                    except httpx.HTTPError as error:
                        api_error(error)
                    else:
                        st.session_state.user_id = user_id.strip()
                        st.session_state.session = SessionResponse.model_validate(result)
                        st.rerun()
    if not session:
        st.info("Enter a user ID in the sidebar to start chatting.")
        return

    with st.chat_message("assistant"):
        chat_text(session.welcome)
    for turn in session.history:
        with st.chat_message("user"):
            chat_text(turn.message)
        render_reply(turn.response)

    suggestion = None
    if not session.history:
        for index, prompt in enumerate(PROMPTS):
            if st.button(prompt, key=f"prompt_{index}"):
                suggestion = prompt
    message = st.chat_input("Ask about cars, features, or a viewing", max_chars=2000) or suggestion
    if not message or not message.strip():
        return
    try:
        with st.spinner("Preparing your reply…"):
            result = api(
                "POST", f"/sessions/{session.session_id}/chat", json={"message": message.strip()}
            )
    except httpx.HTTPError as error:
        api_error(error)
    else:
        response = ChatResponse.model_validate(result)
        session.history.append(Exchange(message=message, response=response))
        st.rerun()


if __name__ == "__main__":
    main()
