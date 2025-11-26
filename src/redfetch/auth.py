# standard
import sys
import webbrowser
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode
import asyncio

# third-party
import httpx
import keyring  # for storing tokens (secrets only)
from keyring.errors import NoKeyringError
import os

# Local
from redfetch import net

# Constants
KEYRING_SERVICE_NAME = 'redfetch'  # Name of your application/service
BASE_URL = net.BASE_URL


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        query_components = parse_qs(urlparse(self.path).query)
        code = query_components.get("code")
        if code:
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b"Authorization successful. You can close this window.")
            self.server.code = code[0]
        else:
            self.send_error(400, "Code not found in the request")


def first_authorization(client_id, client_secret):
    # Step 1: Generate the authorization URL
    params = {
        'response_type': 'code',
        'client_id': client_id,
        'redirect_uri': 'http://127.0.0.1:62897/',
        'scope': 'read'
    }
    auth_url = f"{BASE_URL}/account/authorize?{urlencode(params)}"

    # Attempt to open the URL in the default web browser
    try:
        # `webbrowser.open` returns True if it was able to open the URL
        success = webbrowser.open(auth_url)
        if success:
            print("Please login and authorize the app in your web browser.")
        else:
            raise Exception("Browser could not be opened.")
    except Exception as e:
        # Fallback: Ask the user to manually open the URL
        print("Unable to open the web browser automatically.")
        print("Please open the following URL manually in your browser to authorize the app:")
        print(auth_url)
    
    # Wait for the authorization code via the local server
    authorization_code = run_server()

    # Step 2: Exchange the authorization code for an access token
    token_url = f"{BASE_URL}/redapi/index.php?oauth/token"
    payload = {
        'grant_type': 'authorization_code',
        'code': authorization_code,
        'redirect_uri': 'http://127.0.0.1:62897/',
        'client_id': client_id,
        'client_secret': client_secret
    }
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    response = httpx.post(token_url, headers=headers, data=payload, timeout=10.0)
    if response.is_success:
        token_data = response.json()
        store_tokens_in_keyring(token_data)  # Store tokens securely
        print("Authorization successful and tokens cached.")
        # Step 3: Use the access token to get the user's XenForo API key
        get_xenforo_api_key(token_data['access_token'], token_data['user_id'])
        return True
    else:
        print("Failed to retrieve tokens.")
        print(response.text)
        return False


def get_client_credentials():
    # Yes this is crap, but it's not sensitive. Replacing soon as proper oauth2 finally available in xf 2.3.
    version = 'redfetch'
    try:
        response = httpx.get(f'{BASE_URL}/redapi/credentials.php', params={'version': version}, timeout=10.0)
        response.raise_for_status()  # Raises HTTPStatusError if the response was unsuccessful
        data = response.json()
        return data['client_id'], data['client_secret']
    except httpx.HTTPStatusError as http_err:
        if http_err.response is not None and http_err.response.status_code == 401:
            raise Exception("Authentication failed. The server might be protected by htaccess.") from None
        else:
            raise Exception(f"HTTP error occurred while trying to retrieve client credentials: {http_err}") from None
    except httpx.RequestError as err:
        raise Exception(f"Failed to connect or request error while retrieving client credentials: {err}") from None
    except Exception as e:
        raise Exception(f"An unexpected error occurred: {e}") from None


def authorize():
    # If using env var (mainly for CI), skip OAuth entirely
    if os.environ.get('REDGUIDES_API_KEY'):
        return

    data = get_cached_tokens()

    # Fast path: if we have a cached API key, trust it
    if data.get('api_key') and data.get('user_id'):
        return

    # Try to refresh token if available when the access token is expired or missing
    if data.get('refresh_token') and not token_is_valid():
        try:
            client_id, client_secret = get_client_credentials()
        except Exception as e:
            print(f"Error during authorization: {e}")
            sys.exit(1)
        print("Attempting to refresh token...")
        if refresh_token(client_id, client_secret):
            print("Token refreshed successfully.")
            updated_data = get_cached_tokens()
            if updated_data.get('api_key'):
                return
            print("Warning: Token refreshed but API key not found. Reauthorizing...")

    # Fall back to full authorization (fetch client creds if not already fetched)
    try:
        client_id, client_secret = get_client_credentials()
    except Exception as e:
        print(f"Error during authorization: {e}")
        sys.exit(1)
    print("Performing full authorization...")
    if not first_authorization(client_id, client_secret):
        print("Authorization failed.")
        sys.exit(1)


def run_server():
    server_address = ('', 62897)
    httpd = HTTPServer(server_address, OAuthCallbackHandler)
    httpd.code = None  # Default in case the callback does not set a code
    httpd.handle_request()
    return httpd.code


def store_tokens_in_keyring(data):
    """Store OAuth tokens securely in keyring; store non-secrets in disk cache."""
    from redfetch import api
    
    # Secrets go in keyring
    keyring.set_password(KEYRING_SERVICE_NAME, 'access_token', data['access_token'])
    keyring.set_password(KEYRING_SERVICE_NAME, 'refresh_token', data['refresh_token'])
    
    # Non-sensitive data goes in disk cache
    expires_at = datetime.now().timestamp() + int(data.get('expires_in', 0))
    api.set_token_expiry(str(expires_at))
    api.set_user_id(str(data['user_id']))


def get_xenforo_api_key(access_token, user_id):
    from redfetch import api
    
    api_url = f"{BASE_URL}/redapi/index.php/users/{user_id}/api"
    headers = {
        'Authorization': f'Bearer {access_token}'
    }
    response = httpx.post(api_url, headers=headers, timeout=10.0)
    if response.is_success:
        api_key_data = response.json()
        api_key_value = api_key_data['api_key']
        keyring.set_password(KEYRING_SERVICE_NAME, 'api_key', api_key_value)
        # Blocking call is fine here; this runs before any async event loop is started.
        username = asyncio.run(api.fetch_username(api_key_value))
        if username != "Unknown":
            print("API key and username retrieved and cached.")
        else:
            print("API key retrieved and cached, but username lookup failed.")
    else:
        print("Failed to retrieve API key.")
        print(response.text)


def refresh_token(client_id, client_secret):
    refresh_token_value = keyring.get_password(KEYRING_SERVICE_NAME, 'refresh_token')
    token_url = f"{BASE_URL}/redapi/index.php?oauth/token"
    payload = {
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token_value,
        'client_id': client_id,
        'client_secret': client_secret
    }
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    response = httpx.post(token_url, headers=headers, data=payload, timeout=10.0)
    if response.is_success:
        new_token_data = response.json()
        store_tokens_in_keyring(new_token_data)  # Store refreshed tokens securely
        print("Access token refreshed and cached.")
        return True
    else:
        print("Failed to refresh access token.")
        print(response.text)
        return False


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
    """Retrieve tokens and API key from keyring and disk cache."""
    from redfetch import api
    
    data = {}
    # Secrets from keyring
    data['access_token'] = keyring.get_password(KEYRING_SERVICE_NAME, 'access_token')
    data['refresh_token'] = keyring.get_password(KEYRING_SERVICE_NAME, 'refresh_token')
    data['api_key'] = keyring.get_password(KEYRING_SERVICE_NAME, 'api_key')
    # Non-secrets from disk cache
    data['username'] = api.get_username_from_cache()
    data['user_id'] = api.get_user_id()
    return data


def logout():
    """Clear stored credentials from keyring and all disk caches."""
    from redfetch import api
    from redfetch import meta
    from redfetch import net

    # Clear current secrets from keyring
    keyring_credentials = ['access_token', 'refresh_token', 'api_key']
    # Also clear legacy entries that may exist from older versions
    legacy_credentials = ['user_id', 'username', 'expires_at']
    
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
    authorize()
