"""Simple shared-password authentication for the app.

Every page calls check_password() at the top. If the user hasn't
authenticated, they see a login form and the rest of the page is
blocked from rendering.

Persistence: Streamlit session_state only lives as long as one
websocket session, so restarts and reconnects used to re-prompt.
After a successful login we now also place a TOKEN (salted hash of
the password - never the password itself) in the URL query string.
New sessions that carry the token authenticate silently, so the
password is entered once per browser, not once per session.

Password is read from Streamlit secrets (or env var as fallback).
Never store the password in code.
"""
import hashlib
import os

import streamlit as st

_SALT = "bluemet-url-token-v1"
_PARAM = "k"


def _token(password: str) -> str:
    return hashlib.sha256(
        (_SALT + password).encode("utf-8")
    ).hexdigest()[:24]


def check_password() -> bool:
    """Returns True if the user is authenticated.

    Order of checks: session_state (fast path within a session), then
    the URL token (survives restarts and reconnects). Otherwise shows
    the login form and st.stop()s.
    """
    expected = _get_expected_password()
    if expected is None:
        st.error(
            "\u26a0 Site is not configured with a password. "
            "Contact the administrator."
        )
        st.stop()

    tok = _token(expected)

    # Already authenticated in this session: keep the URL token fresh
    if st.session_state.get("password_correct", False):
        try:
            if st.query_params.get(_PARAM) != tok:
                st.query_params[_PARAM] = tok
        except Exception:
            pass
        return True

    # New session, but the browser URL carries a valid token
    try:
        if st.query_params.get(_PARAM) == tok:
            st.session_state["password_correct"] = True
            return True
    except Exception:
        pass

    # Show login form
    st.markdown("### \U0001f512 Restricted access")
    st.write("This site is for authorized users only.")

    st.markdown(
        """
        <style>
        /* Compact password box: ~16 characters wide */
        [data-testid="stTextInput"] {
            max-width: 200px !important;
        }
        [data-testid="stTextInput"] input {
            font-family: "Courier New", monospace !important;
        }
        /* Hide the "Press Enter to apply" hint */
        [data-testid="InputInstructions"] {
            display: none !important;
        }
        /* Hide the show-password eye toggle (renders as raw
           "visibility" text since the icon font doesn't load) */
        [data-testid="stTextInput"] button {
            display: none !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    password = st.text_input(
        "Enter password:", type="password", key="password_input"
    )

    if password:
        if password == expected:
            st.session_state["password_correct"] = True
            try:
                st.query_params[_PARAM] = tok
            except Exception:
                pass
            st.rerun()
        else:
            st.error("Incorrect password.")

    st.stop()


def _get_expected_password() -> str | None:
    """Get password from Streamlit secrets or environment variable."""
    # Try Streamlit secrets first (works on Streamlit Cloud)
    try:
        if "APP_PASSWORD" in st.secrets:
            return st.secrets["APP_PASSWORD"]
    except Exception:
        pass
    # Fall back to environment variable (works on Render)
    return os.environ.get("APP_PASSWORD")
