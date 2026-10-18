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

18 Oct: a committed HASH can override both. If password.sha256 next
to this file holds a hex digest, the password is whatever hashes to
it (sha256 of _PW_SALT + password) and the host's APP_PASSWORD is
ignored - so the password can be changed with a git push while the
Mac auto-pulls, without touching mac/.env. Make a digest with:
    python3 -c "import hashlib,getpass;print(hashlib.sha256(('bluemet-pw-v1'+getpass.getpass()).encode()).hexdigest())"

The login SCREEN is login_board.py - the network board, with station
condition chips and a fixed-view radar map. This module still owns the
decision; if the board raises for any reason it falls back to a plain
password prompt rather than locking everyone out.
"""
import hashlib
import os

import streamlit as st

_SALT = "bluemet-url-token-v1"
_PARAM = "k"
_PW_SALT = "bluemet-pw-v1"
_HASH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "password.sha256")


def _token(password: str) -> str:
    return hashlib.sha256(
        (_SALT + password).encode("utf-8")
    ).hexdigest()[:24]


def _pw_hash(password: str) -> str:
    return hashlib.sha256((_PW_SALT + password).encode("utf-8")).hexdigest()


def _committed_hash() -> str | None:
    """The digest in password.sha256, or None when the file is absent
    or empty (then the host's APP_PASSWORD applies)."""
    try:
        with open(_HASH_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line.lower() if len(line) == 64 else None
    except Exception:
        pass
    return None


def check_password() -> bool:
    """Returns True if the user is authenticated.

    Order of checks: session_state (fast path within a session), then
    the URL token (survives restarts and reconnects). Otherwise shows
    the login form and st.stop()s.
    """
    committed = _committed_hash()
    expected = None if committed else _get_expected_password()
    if committed is None and expected is None:
        st.error(
            "\u26a0 Site is not configured with a password. "
            "Contact the administrator."
        )
        st.stop()

    # The URL token derives from the digest when one is committed, so
    # every browser is signed out the moment the password changes.
    tok = _token(committed) if committed else _token(expected)

    def _ok(pw: str) -> bool:
        return _pw_hash(pw) == committed if committed else pw == expected

    # Already authenticated in this session: keep the URL token fresh
    if st.session_state.get("password_correct", False):
        try:
            if st.query_params.get(_PARAM) != tok:
                st.query_params[_PARAM] = tok
        except Exception:
            pass
        _duty()
        return True

    # New session, but the browser URL carries a valid token
    try:
        if st.query_params.get(_PARAM) == tok:
            st.session_state["password_correct"] = True
            _duty()
            return True
    except Exception:
        pass

    # Show login form: the network board. Drawing lives in
    # login_board.py; this function keeps the gate.
    try:
        import login_board
        password = login_board.render()
    except Exception:
        # The board must never be the reason nobody can sign in. If it
        # fails to draw for any reason, fall back to the plain prompt.
        st.markdown("### Restricted access")
        st.write("This site is for authorized users only.")
        password = st.text_input(
            "Enter password:", type="password", key="password_input"
        )

    if password:
        if _ok(password):
            st.session_state["password_correct"] = True
            try:
                st.query_params[_PARAM] = tok
            except Exception:
                pass
            st.rerun()
        else:
            st.error("Incorrect password.")

    st.stop()


def _duty() -> None:
    """Duty prompts (14 Oct): role / shift / X-shift, once per session,
    after the password. Never the reason a page fails to draw."""
    try:
        import duty
        duty.gate()
    except Exception as exc:
        # st.stop() / switch_page raise control-flow exceptions that
        # must propagate; anything else is swallowed.
        if type(exc).__name__ in ("StopException", "RerunException", "StreamlitAPIException"):
            raise
        if "Stop" in type(exc).__name__ or "Rerun" in type(exc).__name__:
            raise


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
