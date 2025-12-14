"""Can be used as a standalone script to authorize with RedGuides.

redfetch supports two auth modes:
- API key (via REDGUIDES_API_KEY env var)
- XenForo 2.3 native OAuth2
"""

# standard
import base64
import hashlib
import os
import sys
import time
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

# third-party
import httpx
import keyring  # for storing tokens (secrets only)
from keyring.errors import NoKeyringError

# Local
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
    """Get a setting from env first, then Dynaconf (if initialized)."""
    env_key = f"REDFETCH_{key}"
    env_val = os.environ.get(env_key)
    if env_val not in (None, ""):
        return env_val

    try:
        from redfetch import config

        settings = getattr(config, "settings", None)
        if settings is not None:
            val = settings.get(key, default)
            return default if val in ("", None) else val
    except Exception:
        pass

    return default


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Capture XF OAuth2 redirect responses for loopback redirects."""

    def log_message(self, format, *args):  # noqa: A002 (shadowing built-in 'format')
        # Silence noisy default logging.
        return

    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)

        error = (query.get("error") or [None])[0]
        error_description = (query.get("error_description") or [None])[0]
        code = (query.get("code") or [None])[0]
        state = (query.get("state") or [None])[0]

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


def first_authorization(client_id: str, client_secret: str | None, *, scope: str, redirect_uri: str) -> bool:
    """Perform auth via browser and cache tokens.

    Uses Authorization Code + PKCE (S256) as required by XF for public clients.
    """
    # Step 1: Generate PKCE + state, then build the authorize URL
    state = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
    code_verifier = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")  # 43 chars base64url
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
        print("Failed to retrieve tokens.")
        print(response.text)
        return False

    token_data = response.json()
    if token_data.get("error"):
        print(f"OAuth token error: {token_data.get('error')} {token_data.get('error_description', '')}".strip())
        return False

    # Step 5: Cache tokens and basic user info
    store_tokens_in_keyring(token_data)
    print("Authorization successful and tokens cached.")

    # Cache basic user info (best-effort; not required for API auth).
    try:
        _cache_user_info(token_data.get("access_token"))
    except Exception:
        pass

    return True


def _cache_user_info(access_token: str | None) -> None:
    """Fetch /api/me and cache username/user_id (best-effort)."""
    if not access_token:
        return
    from redfetch import api

    headers = {"Authorization": f"Bearer {access_token}"}
    resp = httpx.get(f"{BASE_URL}/api/me", headers=headers, timeout=10.0)
    resp.raise_for_status()
    data = resp.json()
    me = data.get("me") or {}
    if me.get("username"):
        api.set_username(str(me["username"]))
    if me.get("user_id"):
        api.set_user_id(str(me["user_id"]))


def authorize():
    # If using env var (mainly for CI), skip OAuth entirely
    if os.environ.get('REDGUIDES_API_KEY'):
        return

    client_id = _get_setting("OAUTH_CLIENT_ID")
    client_secret = _get_setting("OAUTH_CLIENT_SECRET", "")  # optional (confidential clients only)
    scope = _get_setting("OAUTH_SCOPE", "user:read resource:read resource:write attachment:write")
    redirect_uri = _get_setting("OAUTH_REDIRECT_URI", DEFAULT_REDIRECT_URI)

    if not client_id:
        print("OAuth client is not configured.")
        print("Set one of the following:")
        print("  - Environment variable: REDFETCH_OAUTH_CLIENT_ID")
        print("  - Or add to your settings.local.toml: OAUTH_CLIENT_ID = \"...\"")
        sys.exit(1)

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
    if not first_authorization(client_id, client_secret, scope=scope, redirect_uri=redirect_uri):
        print("Authorization failed.")
        sys.exit(1)


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
    from redfetch import api
    
    # Secrets go in keyring
    keyring.set_password(KEYRING_SERVICE_NAME, "access_token", data["access_token"])
    keyring.set_password(KEYRING_SERVICE_NAME, "refresh_token", data["refresh_token"])
    
    # Non-sensitive data goes in disk cache
    expires_at = datetime.now().timestamp() + int(data.get("expires_in", 0) or 0)
    api.set_token_expiry(str(expires_at))


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
    from redfetch import api
    
    expires_at_str = api.get_token_expiry()
    if expires_at_str:
        expires_at = datetime.fromtimestamp(float(expires_at_str))
        now = datetime.now()
        is_valid = now < expires_at - timedelta(minutes=5)  # Buffer of 5 minutes
        return is_valid
    else:
        return False


def get_cached_tokens():
    """Retrieve cached OAuth tokens from keyring and non-secrets from disk cache."""
    from redfetch import api
    
    data = {}
    data["access_token"] = keyring.get_password(KEYRING_SERVICE_NAME, "access_token")
    data["refresh_token"] = keyring.get_password(KEYRING_SERVICE_NAME, "refresh_token")
    data["username"] = api.get_username_from_cache()
    data["user_id"] = api.get_user_id()
    return data


def logout():
    """Clear stored credentials from keyring and all disk caches."""
    from redfetch import api
    from redfetch import meta
    from redfetch import net

    # Clear secrets from keyring (including legacy entries that may exist from older versions)
    keyring_credentials = ["access_token", "refresh_token", "api_key"]
    legacy_credentials = ["user_id", "username", "expires_at"]
    
    credentials_deleted = False

    for credential in keyring_credentials + legacy_credentials:
        try:
            keyring.delete_password(KEYRING_SERVICE_NAME, credential)
            credentials_deleted = True
        except keyring.errors.PasswordDeleteError:
            # Credential not found, nothing to delete
            pass

    # Clear the persistent cache directory (API, manifest, PyPI version, etc.)
    try:
        api.clear_api_cache()
        meta.clear_pypi_cache()
        net.clear_manifest_cache()
        credentials_deleted = True
    except Exception:
        pass

    if credentials_deleted:
        print("You have been logged out successfully.")
    else:
        print("No active session found. You were not logged in.")


def initialize_keyring():
    # Skip keyring init if using env var (mainly for CI on Linux)
    if os.environ.get('REDGUIDES_API_KEY'):
        return
    
    try:
        # Attempt to use the keyring to trigger any potential errors
        keyring.get_password('test_service', 'test_user')
    except (NoKeyringError, ModuleNotFoundError):
        print("No suitable keyring backend found, probably because you're not on Windows.")
        print("Please install `keyrings.alt` by running:")
        print("    pip install keyrings.alt")
        print("Then restart the application.")
        sys.exit(1)
    except Exception as e:
        # Catch any other exceptions that may occur and handle them gracefully
        print(f"An error occurred while initializing keyring: {e}")
        print("Please ensure that a suitable keyring backend is available.")
        sys.exit(1)


if __name__ == "__main__":
    initialize_keyring()
    # Initialize config lazily if invoked directly, so this can be used as a standalone script.
    try:
        from redfetch import config

        if getattr(config, "settings", None) is None:
            config.initialize_config()
    except Exception:
        pass
    authorize()
