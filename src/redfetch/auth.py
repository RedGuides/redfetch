"""Can be used as a standalone script to authorize with RedGuides.

redfetch supports two auth modes:
- API key (via REDGUIDES_API_KEY env var)
- XenForo 2.3 native OAuth2
"""

# standard
import base64
import hashlib
import asyncio
import os
import secrets
import time
import webbrowser
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qsl, urlencode, urlparse

# third-party
import httpx
import keyring  # for storing tokens (secrets only)
from keyring.errors import NoKeyringError, PasswordDeleteError

# Local
from redfetch import cache
from redfetch import config
from redfetch import net

# Constants
KEYRING_SERVICE_NAME = "redfetch"
BASE_URL = net.BASE_URL

AUTHORIZATION_ENDPOINT = f"{BASE_URL}/oauth2/authorize"
TOKEN_ENDPOINT = f"{BASE_URL}/api/oauth2/token"

# Loopback redirect default (must match the OAuth client redirect URI exactly)
DEFAULT_REDIRECT_URI = "http://127.0.0.1:62897/"
DEFAULT_LOOPBACK_PORT = 62897
_REFRESH_CODE_VERIFIER = "refresh"  # XF 2.3 requires a non-empty code_verifier even for refresh (public clients)


def _get_setting(key: str, default=None):
    """REDFETCH_* env vars have top precedence thanks to dynaconf."""
    if config.settings is None:  # standalone-script mode, before config init
        return os.environ.get(f"REDFETCH_{key}") or default
    val = config.settings.get(key, default)
    return default if val in ("", None) else val


# ---------------------------------------------------------------------------
# Non-secret cached identity data
# ---------------------------------------------------------------------------

def set_username(username: str) -> None:
    """Store username in disk cache (non-sensitive public display name)."""
    cache.shared().set('username', username)


def get_username_from_cache() -> str | None:
    """Retrieve username from disk cache."""
    return cache.shared().get('username')


def set_token_expiry(expires_at: str) -> None:
    """Store OAuth token expiry timestamp in disk cache."""
    cache.shared().set('expires_at', expires_at)


def get_token_expiry() -> str | None:
    """Retrieve OAuth token expiry timestamp from disk cache."""
    return cache.shared().get('expires_at')


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Capture XF OAuth2 redirect responses for loopback redirects."""

    def log_message(self, format, *args):  # noqa: A002 (shadowing built-in 'format')
        # Silence noisy default logging.
        return

    def do_GET(self):
        query = dict(parse_qsl(urlparse(self.path).query))

        error = query.get("error")
        error_description = query.get("error_description")
        code = query.get("code")
        state = query.get("state")

        # Some browsers will request /favicon.ico or similar first; ignore those.
        if not error and (not code or not state):
            self.send_response(404)
            self.send_header("Content-type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Waiting for OAuth response...")
            return

        if error:
            self.server.error = f"{error} {error_description or ''}".strip()
            self.send_response(200)
            self.send_header("Content-type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Authorization failed. You can close this tab.")
            return

        self.server.code = code
        self.server.state = state
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Authorization successful. You can close this tab.")


def first_authorization(client_id: str, client_secret: str | None, *, scope: str, redirect_uri: str) -> None:
    """Perform auth via browser and cache tokens.

    Uses Authorization Code + PKCE (S256) as required by XF for public clients.
    """
    # Step 1: Generate PKCE + state, then build the authorize URL
    state = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(32) 
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope or "",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{AUTHORIZATION_ENDPOINT}?{urlencode(params)}"

    # Step 2: Open the authorize URL in the user's browser
    try:
        success = webbrowser.open(auth_url)
        if success:
            print("Please login and authorize the app in your web browser.")
        else:
            raise RuntimeError("Browser could not be opened.")
    except Exception:
        print("Unable to open the web browser automatically.")
        print("Please open the following URL manually in your browser to authorize the app:")
        print(auth_url)

    # Step 3: Wait for the authorization code via the loopback redirect
    authorization_code = run_server(expected_state=state, redirect_uri=redirect_uri)

    # Step 4: Exchange the authorization code for tokens
    payload = {
        "client_id": client_id,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
        "code": authorization_code,
        "code_verifier": code_verifier,
    }
    if client_secret:
        payload["client_secret"] = client_secret

    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    response = httpx.post(TOKEN_ENDPOINT, headers=headers, data=payload, timeout=10.0)
    if not response.is_success:
        details = response.text.strip()
        if details:
            raise RuntimeError(f"Failed to retrieve tokens.\n{details}")
        raise RuntimeError("Failed to retrieve tokens.")

    token_data = response.json()
    if token_data.get("error"):
        raise RuntimeError(
            f"OAuth token error: {token_data.get('error')} {token_data.get('error_description', '')}".strip()
        )

    # Step 5: Cache tokens and basic user info
    store_tokens_in_keyring(token_data)
    print("Authorization successful and tokens cached.")

    # Cache basic user info (best-effort; not required for API auth).
    with suppress(Exception):
        _cache_user_info(token_data.get("access_token"))


def _cache_user_info(access_token: str | None) -> None:
    """Fetch /api/me and cache username (best-effort)."""
    if not access_token:
        return
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = httpx.get(f"{BASE_URL}/api/me", headers=headers, timeout=10.0)
    resp.raise_for_status()
    data = resp.json()
    me = data.get("me") or {}
    if me.get("username"):
        set_username(str(me["username"]))


def authorize():
    # If using env var (mainly for CI), skip OAuth entirely
    if os.environ.get('REDGUIDES_API_KEY'):
        return

    client_id = _get_setting("OAUTH_CLIENT_ID")
    client_secret = _get_setting("OAUTH_CLIENT_SECRET", "")  # optional (confidential clients only)
    scope = _get_setting("OAUTH_SCOPE", "user:read resource:read resource:write attachment:write")
    redirect_uri = _get_setting("OAUTH_REDIRECT_URI", DEFAULT_REDIRECT_URI)

    if not client_id:
        raise RuntimeError("OAuth client is not configured.")

    data = get_cached_tokens()

    # Fast path: valid access token already cached
    if data.get("access_token") and token_is_valid():
        return

    # Try refresh if we have a refresh token
    if data.get("refresh_token"):
        print("Attempting to refresh access token...")
        if refresh_token(client_id, client_secret, redirect_uri=redirect_uri):
            print("Token refreshed successfully.")
            return

    # Fall back to interactive authorization
    print("Performing full authorization...")
    first_authorization(client_id, client_secret, scope=scope, redirect_uri=redirect_uri)


def _port_from_redirect_uri(redirect_uri: str) -> int:
    try:
        parsed = urlparse(redirect_uri)
        if parsed.port:
            return int(parsed.port)
    except Exception:
        pass
    return DEFAULT_LOOPBACK_PORT


def run_server(*, expected_state: str, redirect_uri: str, timeout_seconds: int = 300) -> str:
    """Start a loopback HTTP server and wait for XF's OAuth redirect."""
    port = _port_from_redirect_uri(redirect_uri)
    server_address = ("127.0.0.1", port)
    httpd = HTTPServer(server_address, OAuthCallbackHandler)
    httpd.timeout = 5  # allow periodic timeout checks
    httpd.code = None
    httpd.state = None
    httpd.error = None

    start = time.time()
    while True:
        if httpd.error:
            raise RuntimeError(f"OAuth authorization error: {httpd.error}")
        if httpd.code:
            break
        if time.time() - start > timeout_seconds:
            raise TimeoutError("Timed out waiting for OAuth authorization response.")
        httpd.handle_request()

    if httpd.state != expected_state:
        raise RuntimeError("Received OAuth response with invalid state.")

    return httpd.code


def store_tokens_in_keyring(data):
    """Store OAuth tokens securely in keyring; store non-secrets in disk cache."""
    keyring.set_password(KEYRING_SERVICE_NAME, "access_token", data["access_token"])
    keyring.set_password(KEYRING_SERVICE_NAME, "refresh_token", data["refresh_token"])
    
    expires_at = time.time() + int(data.get("expires_in", 0) or 0)
    set_token_expiry(str(expires_at))


def refresh_token(client_id: str, client_secret: str | None, *, redirect_uri: str) -> bool:
    refresh_token_value = keyring.get_password(KEYRING_SERVICE_NAME, "refresh_token")
    if not refresh_token_value:
        return False

    payload = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "redirect_uri": redirect_uri,
        "refresh_token": refresh_token_value,
        # XF 2.3 requires code_verifier for public clients even on refresh. A static non-empty value works.
        "code_verifier": _REFRESH_CODE_VERIFIER,
    }
    if client_secret:
        payload["client_secret"] = client_secret

    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    response = httpx.post(TOKEN_ENDPOINT, headers=headers, data=payload, timeout=10.0)
    if not response.is_success:
        print("Failed to refresh access token.")
        print(response.text)
        return False

    new_token_data = response.json()
    if new_token_data.get("error"):
        print(f"OAuth token error: {new_token_data.get('error')} {new_token_data.get('error_description', '')}".strip())
        return False

    store_tokens_in_keyring(new_token_data)
    return True


def token_is_valid():
    """Check if the access token is still valid."""
    expires_at = get_token_expiry()
    # 5-minute buffer
    return bool(expires_at) and time.time() < float(expires_at) - 300


def get_cached_tokens():
    """Retrieve cached OAuth tokens from keyring and non-secrets from disk cache."""
    data = {}
    data["access_token"] = keyring.get_password(KEYRING_SERVICE_NAME, "access_token")
    data["refresh_token"] = keyring.get_password(KEYRING_SERVICE_NAME, "refresh_token")
    data["username"] = get_username_from_cache()
    return data


def logout():
    """Clear stored credentials."""
    keyring_credentials = ["access_token", "refresh_token", "api_key"]
    legacy_credentials = ["user_id", "username", "expires_at"]

    for credential in keyring_credentials + legacy_credentials:
        with suppress(PasswordDeleteError):
            keyring.delete_password(KEYRING_SERVICE_NAME, credential)

    with suppress(Exception):  # cache cleanup must not block logout
        cache.clear()


# ---------------------------------------------------------------------------
# API identity resolution
# ---------------------------------------------------------------------------

async def fetch_me(client: httpx.AsyncClient) -> dict | None:
    """Fetch current user info from /api/me."""
    url = f'{BASE_URL}/api/me'
    try:
        data = await net.get_json(client, url)
        return {
            'user_id': str(data['me']['user_id']),
            'username': data['me']['username']
        }
    except Exception as e:
        print(f"Failed to retrieve user info: {e}")
        return None


async def fetch_user_id_from_api(api_key):
    """Fetch user_id using the API key."""
    async with httpx.AsyncClient(headers={'XF-Api-Key': api_key}, http2=True) as client:
        me = await fetch_me(client)
    if me:
        return me['user_id']
    return None


async def fetch_username(api_key, cache=True):
    """Fetch username via API key; caches username."""
    async with httpx.AsyncClient(headers={'XF-Api-Key': api_key}, http2=True) as client:
        me = await fetch_me(client)
    if me:
        if cache:
            set_username(me['username'])
        return me['username']
    return "Unknown"


async def get_api_headers():
    """Return auth headers for XenForo API requests.

    Priority order:
    1) API key via env: `REDGUIDES_API_KEY`
    2) Native OAuth2: cached `access_token` from keyring
    """
    api_key = os.environ.get('REDGUIDES_API_KEY')
    if api_key:
        headers = {'XF-Api-Key': api_key}
        user_id = os.environ.get('REDGUIDES_USER_ID')
        if not user_id:
            user_id = await fetch_user_id_from_api(api_key)
            if not user_id:
                raise RuntimeError("Unable to retrieve user ID using the provided API key.")
        headers['XF-Api-User'] = str(user_id)
        return headers

    access_token = keyring.get_password(KEYRING_SERVICE_NAME, "access_token")
    refresh_tok = keyring.get_password(KEYRING_SERVICE_NAME, "refresh_token")

    if access_token or refresh_tok:
        if access_token and token_is_valid():
            return {"Authorization": f"Bearer {access_token}"}

        if refresh_tok:
            client_id = _get_setting("OAUTH_CLIENT_ID")
            client_secret = _get_setting("OAUTH_CLIENT_SECRET", "")
            redirect_uri = _get_setting("OAUTH_REDIRECT_URI", DEFAULT_REDIRECT_URI)

            if not client_id:
                raise RuntimeError("OAuth client is not configured.")

            refreshed = await asyncio.to_thread(
                refresh_token,
                str(client_id),
                str(client_secret or ""),
                redirect_uri=str(redirect_uri),
            )
            if refreshed:
                access_token = keyring.get_password(KEYRING_SERVICE_NAME, "access_token")
                if access_token:
                    return {"Authorization": f"Bearer {access_token}"}

            raise RuntimeError("OAuth token refresh failed. Restart redfetch to authorize again.")

        raise RuntimeError("OAuth access token is expired and no refresh token is available. Please authorize again.")

    raise RuntimeError(
        "Not authenticated. Set REDGUIDES_API_KEY (and optionally REDGUIDES_USER_ID), "
        "or authorize via OAuth."
    )


async def get_username():
    """Fetch the username from the environment variable, disk cache, or API."""
    username = os.environ.get('REDFETCH_USERNAME')
    if username:
        return username

    username = get_username_from_cache()
    if username:
        return username

    api_key = os.environ.get('REDGUIDES_API_KEY')
    if api_key:
        username = await fetch_username(api_key)
        if username != "Unknown":
            return username
        raise RuntimeError("Unable to retrieve username using the provided API key.")

    access_token = keyring.get_password(KEYRING_SERVICE_NAME, "access_token")
    refresh_tok = keyring.get_password(KEYRING_SERVICE_NAME, "refresh_token")
    if access_token or refresh_tok:
        headers = await get_api_headers()
        async with httpx.AsyncClient(headers=headers, http2=True) as client:
            me = await fetch_me(client)
        if me and me.get("username"):
            set_username(me["username"])
            return me["username"]
        raise RuntimeError("Unable to retrieve username using the stored OAuth token.")

    raise RuntimeError("Username not found. Set REDGUIDES_API_KEY or authorize via OAuth.")


def initialize_keyring():
    # Skip keyring init if using env var (mainly for CI on Linux)
    if os.environ.get('REDGUIDES_API_KEY'):
        return
    
    try:
        # Attempt to use the keyring to trigger any potential errors
        keyring.get_password('test_service', 'test_user')
    except (NoKeyringError, ModuleNotFoundError):
        raise RuntimeError(
            "No suitable keyring backend found, probably because you're not on Windows.\n\n"
            "Please install `keyrings.alt` by running:\n"
            "    pipx inject redfetch keyrings.alt\n\n"
            "Then restart the application."
        )
    except Exception as e:
        # Catch any other exceptions that may occur and handle them gracefully
        raise RuntimeError(
            f"An error occurred while initializing keyring: {e}\n\n"
            "Please ensure that a suitable keyring backend is available."
        ) from e


if __name__ == "__main__":
    initialize_keyring()
    if not os.environ.get("REDGUIDES_API_KEY"):
        # Initialize config lazily if invoked directly, so this can be used as a standalone script.
        if config.settings is None:
            config.initialize_config()
    authorize()
