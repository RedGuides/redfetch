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
import time
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse

# third-party
import httpx
import keyring  # for storing tokens (secrets only)
from diskcache import Cache
from keyring.errors import NoKeyringError

# Local
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
    """Get a setting from env first, then initialized Dynaconf settings."""
    env_key = f"REDFETCH_{key}"
    env_val = os.environ.get(env_key)
    if env_val not in (None, ""):
        return env_val

    if config.settings is not None:
        val = config.settings.get(key, default)
        return default if val in ("", None) else val

    return default


# ---------------------------------------------------------------------------
# Disk cache for non-secret identity data (user_id, username, token_expiry)
# ---------------------------------------------------------------------------

_disk_cache = None


def _get_disk_cache_instance():
    """Create a diskcache.Cache in the config directory."""
    cache_dir = getattr(config, 'config_dir', None) or os.getenv('REDFETCH_CONFIG_DIR')
    if not cache_dir:
        cache_dir = os.getcwd()
    return Cache(os.path.join(cache_dir, '.cache'))


def _ensure_cache():
    global _disk_cache
    if _disk_cache is None:
        _disk_cache = _get_disk_cache_instance()
    return _disk_cache


def get_disk_cache():
    """Return the shared disk cache (for non-identity caching by other modules)."""
    return _ensure_cache()


def set_user_id(user_id: str) -> None:
    """Store user_id in disk cache (non-sensitive public identifier)."""
    _ensure_cache().set('user_id', str(user_id))


def get_user_id() -> Optional[str]:
    """Retrieve user_id from disk cache."""
    return _ensure_cache().get('user_id')


def set_username(username: str) -> None:
    """Store username in disk cache (non-sensitive public display name)."""
    _ensure_cache().set('username', username)


def get_username_from_cache() -> Optional[str]:
    """Retrieve username from disk cache."""
    return _ensure_cache().get('username')


def set_token_expiry(expires_at: str) -> None:
    """Store OAuth token expiry timestamp in disk cache."""
    _ensure_cache().set('expires_at', expires_at)


def get_token_expiry() -> Optional[str]:
    """Retrieve OAuth token expiry timestamp from disk cache."""
    return _ensure_cache().get('expires_at')


def clear_disk_cache():
    """Clear all cached non-secret data."""
    global _disk_cache
    cache = _ensure_cache()
    try:
        cache.clear()
    finally:
        try:
            cache.close()
        except Exception:
            pass
        _disk_cache = None


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


def first_authorization(client_id: str, client_secret: str | None, *, scope: str, redirect_uri: str) -> None:
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
    try:
        _cache_user_info(token_data.get("access_token"))
    except Exception:
        pass


def _cache_user_info(access_token: str | None) -> None:
    """Fetch /api/me and cache username/user_id (best-effort)."""
    if not access_token:
        return
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = httpx.get(f"{BASE_URL}/api/me", headers=headers, timeout=10.0)
    resp.raise_for_status()
    data = resp.json()
    me = data.get("me") or {}
    if me.get("username"):
        set_username(str(me["username"]))
    if me.get("user_id"):
        set_user_id(str(me["user_id"]))


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
    
    expires_at = datetime.now().timestamp() + int(data.get("expires_in", 0) or 0)
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
    expires_at_str = get_token_expiry()
    if expires_at_str:
        expires_at = datetime.fromtimestamp(float(expires_at_str))
        now = datetime.now()
        is_valid = now < expires_at - timedelta(minutes=5)  # Buffer of 5 minutes
        return is_valid
    else:
        return False


def get_cached_tokens():
    """Retrieve cached OAuth tokens from keyring and non-secrets from disk cache."""
    data = {}
    data["access_token"] = keyring.get_password(KEYRING_SERVICE_NAME, "access_token")
    data["refresh_token"] = keyring.get_password(KEYRING_SERVICE_NAME, "refresh_token")
    data["username"] = get_username_from_cache()
    data["user_id"] = get_user_id()
    return data


def logout():
    """Clear stored credentials from keyring and all disk caches."""
    from redfetch import meta

    keyring_credentials = ["access_token", "refresh_token", "api_key"]
    legacy_credentials = ["user_id", "username", "expires_at"]
    
    credentials_deleted = False

    for credential in keyring_credentials + legacy_credentials:
        try:
            keyring.delete_password(KEYRING_SERVICE_NAME, credential)
            credentials_deleted = True
        except keyring.errors.PasswordDeleteError:
            pass

    try:
        clear_disk_cache()
        meta.clear_pypi_cache()
        net.clear_manifest_cache()
        credentials_deleted = True
    except Exception:
        pass

    if credentials_deleted:
        print("You have been logged out successfully.")
    else:
        print("No active session found. You were not logged in.")


# ---------------------------------------------------------------------------
# API identity resolution
# ---------------------------------------------------------------------------

async def fetch_me(client: httpx.AsyncClient) -> Optional[dict]:
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
    """Fetch user_id using the API key; caches it."""
    async with httpx.AsyncClient(headers={'XF-Api-Key': api_key}, http2=True) as client:
        me = await fetch_me(client)
    if me:
        set_user_id(me['user_id'])
        return me['user_id']
    return None


async def fetch_username(api_key, cache=True):
    """Fetch username via API key; caches username and user_id."""
    async with httpx.AsyncClient(headers={'XF-Api-Key': api_key}, http2=True) as client:
        me = await fetch_me(client)
    if me:
        if cache:
            set_username(me['username'])
            set_user_id(me['user_id'])
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

            raise RuntimeError("OAuth token refresh failed. Please run `redfetch logout` and authorize again.")

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
            set_user_id(me["user_id"])
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
            "    pip install keyrings.alt\n\n"
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
