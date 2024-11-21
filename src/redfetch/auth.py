# standard
import sys
import webbrowser
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode

# third-party
import requests
import keyring  # for storing tokens
from keyring.errors import NoKeyringError
import os

# Constants
KEYRING_SERVICE_NAME = 'redfetch'  # Name of your application/service
BASE_URL = os.environ.get('REDFETCH_BASE_URL', 'https://www.redguides.com/community')

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
    response = requests.post(token_url, headers=headers, data=payload)
    if response.ok:
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
        response = requests.get(f'{BASE_URL}/redapi/credentials.php?version={version}')
        response.raise_for_status()  # Raises HTTPError if the response was unsuccessful
        data = response.json()
        return data['client_id'], data['client_secret']
    except requests.exceptions.HTTPError as http_err:
        if response.status_code == 401:
            raise Exception("Authentication failed. The server might be protected by htaccess.") from None
        else:
            raise Exception(f"HTTP error occurred while trying to retrieve client credentials: {http_err}") from None
    except requests.exceptions.ConnectionError:
        raise Exception("Failed to connect to the server. It might be down or unreachable.") from None
    except requests.exceptions.RequestException as err:
        raise Exception(f"An error occurred while trying to retrieve client credentials: {err}") from None
    except Exception as e:
        raise Exception(f"An unexpected error occurred: {e}") from None

def authorize():
    try:
        client_id, client_secret = get_client_credentials()
    except Exception as e:
        print(f"Error during authorization: {e}")
        sys.exit(1)

    data = get_cached_tokens()  # Load existing tokens from keyring
    if data.get('api_key') and api_is_valid(data['api_key']):
        return  # Exit if the API key is valid and cached
    else:
        print("Looks like we need to get a new API key.")
        if token_is_valid():
            print("Token is still valid, attempting to refresh or reauthorize for a new API key.")
            if data.get('refresh_token'):
                if refresh_token(data, client_id, client_secret):
                    print("Token successfully refreshed.")
                    updated_data = get_cached_tokens()
                    if updated_data.get('api_key') and api_is_valid(updated_data['api_key']):
                        return  # Exit if the API key is found and valid after refreshing
                    else:
                        print("API key still invalid after refresh, reauthorizing...")
                        first_authorization(client_id, client_secret)
                else:
                    print("Token refresh failed, proceeding to first authorization.")
                    first_authorization(client_id, client_secret)
            else:
                print("Refresh token not found, proceeding to first authorization.")
                first_authorization(client_id, client_secret)
        else:
            if not first_authorization(client_id, client_secret):
                print("First authorization failed.")

def run_server():
    server_address = ('', 62897)
    httpd = HTTPServer(server_address, OAuthCallbackHandler)
    httpd.handle_request()
    return httpd.code

def fetch_username(api_key):
    """This is duplicate code from api.py, but leaving it here for now."""
    url = f"{BASE_URL}/api/me/"
    headers = {
        'XF-Api-Key': api_key
    }
    response = requests.get(url, headers=headers)
    if response.ok:
        username = response.json()['me']['username']
        print(f"Hail and well met, {username}")
        return username
    else:
        print("Failed to retrieve username.")
        print(response.text)
        return "Unknown"

def store_tokens_in_keyring(data):
    """Store tokens securely in keyring."""
    keyring.set_password(KEYRING_SERVICE_NAME, 'access_token', data['access_token'])
    keyring.set_password(KEYRING_SERVICE_NAME, 'refresh_token', data['refresh_token'])
    expires_at = datetime.now().timestamp() + int(data.get('expires_in', 0))
    keyring.set_password(KEYRING_SERVICE_NAME, 'expires_at', str(expires_at))
    keyring.set_password(KEYRING_SERVICE_NAME, 'user_id', str(data['user_id']))

def get_xenforo_api_key(access_token, user_id):
    api_url = f"{BASE_URL}/redapi/index.php/users/{user_id}/api"
    headers = {
        'Authorization': f'Bearer {access_token}'
    }
    response = requests.post(api_url, headers=headers)
    if response.ok:
        api_key_data = response.json()
        username = fetch_username(api_key_data['api_key'])  # Fetch the username using the API key
        keyring.set_password(KEYRING_SERVICE_NAME, 'api_key', api_key_data['api_key'])
        keyring.set_password(KEYRING_SERVICE_NAME, 'username', username)
        print("API key and username retrieved and saved to keyring.")
    else:
        print("Failed to retrieve API key.")
        print(response.text)

def refresh_token(data, client_id, client_secret):
    refresh_token = keyring.get_password(KEYRING_SERVICE_NAME, 'refresh_token')
    token_url = f"{BASE_URL}/redapi/index.php?oauth/token"
    payload = {
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token,
        'client_id': client_id,
        'client_secret': client_secret
    }
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    response = requests.post(token_url, headers=headers, data=payload)
    if response.ok:
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
    expires_at_str = keyring.get_password(KEYRING_SERVICE_NAME, 'expires_at')
    if expires_at_str:
        expires_at = datetime.fromtimestamp(float(expires_at_str))
        now = datetime.now()
        is_valid = now < expires_at - timedelta(minutes=5)  # Buffer of 5 minutes
        print(f"Checking token validity: now={now}, expires_at={expires_at}, is_valid={is_valid}")
        return is_valid
    else:
        print("Expires_at not found in keyring.")
        return False

def api_is_valid(api_key):
    """Validate the stored API key."""
    username = fetch_username(api_key)
    if username != "Unknown":
        return True
    else:
        print("API key validation failed.")
        return False

def get_cached_tokens():
    """Retrieve tokens and API key from keyring."""
    data = {}
    data['access_token'] = keyring.get_password(KEYRING_SERVICE_NAME, 'access_token')
    data['refresh_token'] = keyring.get_password(KEYRING_SERVICE_NAME, 'refresh_token')
    data['expires_at'] = keyring.get_password(KEYRING_SERVICE_NAME, 'expires_at')
    data['api_key'] = keyring.get_password(KEYRING_SERVICE_NAME, 'api_key')
    data['username'] = keyring.get_password(KEYRING_SERVICE_NAME, 'username')
    data['user_id'] = keyring.get_password(KEYRING_SERVICE_NAME, 'user_id')
    return data

def logout():
    """Clear stored credentials from keyring."""
    credentials = ['access_token', 'refresh_token', 'expires_at', 'api_key', 'username', 'user_id']
    credentials_deleted = False

    for credential in credentials:
        try:
            keyring.delete_password(KEYRING_SERVICE_NAME, credential)
            credentials_deleted = True
        except keyring.errors.PasswordDeleteError:
            # Credential not found, nothing to delete
            pass

    if credentials_deleted:
        print("You have been logged out successfully.")
    else:
        print("No active session found. You were not logged in.")

def initialize_keyring():
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
