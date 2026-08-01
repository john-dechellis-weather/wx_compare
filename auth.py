"""Simple shared-password authentication for the app.

Every page calls check_password() at the top. If the user hasn't
authenticated in this session, they see a login form and the rest of
the page is blocked from rendering.

Password is read from Streamlit secrets (or env var as fallback).
Never store the password in code.
"""
import os
import streamlit as st


def check_password() -> bool:
    """Returns True if the user has entered the correct password.

    If not authenticated, displays the login form and calls st.stop()
    so the rest of the page doesn't render.
    """
    # Already authenticated in this session
    if st.session_state.get("password_correct", False):
        return True

    # Get the expected password from secrets or env
    expected = _get_expected_password()
    if expected is None:
        st.error(
            "⚠ Site is not configured with a password. "
            "Contact the administrator."
        )
        st.stop()

    # Show login form
    st.markdown("### 🔒 Restricted access")
    st.write("This site is for authorized users only.")

    password = st.text_input("Enter password:", type="password", key="password_input")

    if password:
        if password == expected:
            st.session_state["password_correct"] = True
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