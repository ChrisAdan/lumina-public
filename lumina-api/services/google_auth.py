"""
services/google_auth.py
Shared Google OAuth2 token manager.
All Google service modules import get_credentials() from here.

First run: call this file directly to trigger the browser auth flow.
    python services/google_auth.py
Subsequent runs: token refreshes silently using the stored token.json.
"""
import os
import logging
import stat
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from configs.app import GOOGLE_CREDENTIALS_PATH, GOOGLE_TOKEN_PATH

logger = logging.getLogger(__name__)

# If you change scopes, delete token.json and re-auth.
SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/drive.readonly",
]


def get_credentials() -> Credentials:
    """
    Return valid Google OAuth2 credentials, refreshing or re-authorising as needed.

    Raises:
        FileNotFoundError: if credentials.json is missing (GCP setup not done).
        RuntimeError: if headless refresh fails and interactive auth is required.
    """
    creds_path = Path(GOOGLE_CREDENTIALS_PATH)
    token_path = Path(GOOGLE_TOKEN_PATH)

    if not creds_path.exists():
        raise FileNotFoundError(
            f"Google credentials not found at {creds_path}. "
            "Download credentials.json from GCP Console and place it there."
        )

    creds: Credentials | None = None

    # Load existing token if present
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    # Refresh if expired
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            logger.info("Google token refreshed successfully")
        except Exception as exc:
            logger.warning("Token refresh failed, re-auth required: %s", exc)
            creds = None

    # Interactive auth flow (first run or after refresh failure)
    if not creds or not creds.valid:
        if os.environ.get("ENV") == "production":
            raise RuntimeError(
                "Google token is invalid and ENV=production — cannot open browser. "
                "Run `python services/google_auth.py` interactively once to re-auth."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
        # Pinned port so the OAuth callback can be forwarded over SSH cleanly
        # (port=0 picks a random port each run, which breaks `ssh -L`).
        # open_browser=False suits headless hosts — the script prints the URL
        # and the user opens it on a machine with a browser. The forwarded
        # localhost:8090 callback still lands back here.
        creds = flow.run_local_server(port=8090, open_browser=False)
        logger.info("Google auth flow completed, token written to %s", token_path)

    # Persist refreshed token
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    token_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    return creds


if __name__ == "__main__":
    # Two-phase copy-paste auth — no redirect server, no PKCE, works inside Docker.
    # Phase 1 (no args):        prints auth URL, saves state to /tmp/.lumina_oauth_state
    # Phase 2 (<code> as arg):  exchanges code for token and writes token.json
    import json
    import sys
    import requests
    from requests_oauthlib import OAuth2Session

    logging.basicConfig(level=logging.INFO)
    creds_path = Path(GOOGLE_CREDENTIALS_PATH)
    token_path = Path(GOOGLE_TOKEN_PATH)
    state_path = Path("/tmp/.lumina_oauth_state")

    if not creds_path.exists():
        print(f"ERROR: {creds_path} not found.", file=sys.stderr)
        sys.exit(1)

    client_data = json.loads(creds_path.read_text())["installed"]
    CLIENT_ID     = client_data["client_id"]
    CLIENT_SECRET = client_data["client_secret"]
    AUTH_URI      = client_data["auth_uri"]
    TOKEN_URI     = client_data["token_uri"]
    REDIRECT_URI  = "urn:ietf:wg:oauth:2.0:oob"

    if len(sys.argv) == 1:
        # Phase 1 — generate URL (no PKCE)
        session = OAuth2Session(client_id=CLIENT_ID, scope=SCOPES, redirect_uri=REDIRECT_URI)
        auth_url, state = session.authorization_url(AUTH_URI, access_type="offline", prompt="consent")
        state_path.write_text(json.dumps({"state": state}))
        print("\n--- OPEN THIS URL IN YOUR BROWSER ---")
        print(auth_url)
        print("-------------------------------------")
        print("\nThen run:  python services/google_auth.py <paste-code-here>")
    else:
        # Phase 2 — exchange code for token
        code = sys.argv[1].strip()
        if not state_path.exists():
            print("ERROR: no saved state — run without args first.", file=sys.stderr)
            sys.exit(1)
        saved = json.loads(state_path.read_text())
        session = OAuth2Session(client_id=CLIENT_ID, scope=SCOPES,
                                redirect_uri=REDIRECT_URI, state=saved["state"])
        import os
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"  # OOB is not https
        token = session.fetch_token(
            TOKEN_URI,
            code=code,
            client_secret=CLIENT_SECRET,
        )
        # Build a Credentials object so token.json is compatible with google-auth
        from google.oauth2.credentials import Credentials
        creds = Credentials(
            token=token["access_token"],
            refresh_token=token.get("refresh_token"),
            token_uri=TOKEN_URI,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            scopes=SCOPES,
        )
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        token_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        state_path.unlink(missing_ok=True)
        print(f"\nAuth successful. Token written to {GOOGLE_TOKEN_PATH}")
        print(f"Access token present: {bool(creds.token)}")
        print(f"Refresh token present: {bool(creds.refresh_token)}")
