import logging
import time
from typing import Optional
import aiohttp
from aiohttp import ClientSession, CookieJar
from galaxy.http import HttpClient
from yarl import URL

from galaxy.api.errors import AccessDenied, AuthenticationRequired, BackendError, NetworkError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class CookieJar(aiohttp.CookieJar):
    def __init__(self):
        super().__init__()
        self._cookies_updated_callback = None

    def set_cookies_updated_callback(self, callback):
        self._cookies_updated_callback = callback

    def update_cookies(self, cookies, url=URL()):
        super().update_cookies(cookies, url)
        if cookies and self._cookies_updated_callback:
            self._cookies_updated_callback(list(self))


class AuthenticatedHttpClient(HttpClient):
    def __init__(self):
        self._auth_lost_callback = None
        self._cookie_jar = CookieJar()
        self._access_token = None
        self._refresh_token = None
        self._last_access_token_success = None
        self._save_last_callback = None
        self._session = ClientSession(cookie_jar=self._cookie_jar)

    def set_auth_lost_callback(self, callback):
        self._auth_lost_callback = callback

    def set_cookies_updated_callback(self, callback):
        self._cookie_jar.set_cookies_updated_callback(callback)

    async def authenticate(self, cookies):
        self._cookie_jar.update_cookies(cookies)
        if self._last_access_token_success < int(time.time()) - 259199:
            await self._refresh_access_token()
        else:
            await self._get_access_token()

    def is_authenticated(self):
        return self._access_token is not None

    async def get(self, *args, **kwargs):
        if not self._access_token:
            raise AccessDenied("No access token")
        try:
            return await self._authorized_get(*args, **kwargs)
        except (AuthenticationRequired, AccessDenied):
            await self._refresh_token()
            return await self._authorized_get(*args, **kwargs)
        
    async def post(self, *args, **kwargs):
        if not self._access_token:
            raise AccessDenied("No access token")
        try:
            return await self._authorized_post(*args, **kwargs)
        except (AuthenticationRequired, AccessDenied):
            await self._refresh_token()
            return await self._authorized_post(*args, **kwargs)

    async def _authorized_get(self, url, *args, **kwargs):
        headers = kwargs.setdefault("headers", {})
        headers["Authorization"] = "Bearer {}".format(self._access_token)
        async with self._session.get(url, *args, **kwargs) as response:
            response.raise_for_status()
            return await response.json()
    
    async def _authorized_post(self, url, *args, **kwargs):
        headers = kwargs.setdefault("headers", {})
        headers["Authorization"] = "Bearer {}".format(self._access_token)
        async with self._session.post(url, *args, **kwargs) as response:
            response.raise_for_status()
            return await response.json()

    async def _exchange_code_for_token(self, code: str):
        token_url = "https://accounts.ea.com/connect/token"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        token_params = {
            "token_format": "JWS",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "grant_type": "authorization_code",
            "redirect_uri": "qrc:///html/login_successful.html",
            "code": code
        }
        try:
            async with self._session.post(token_url, headers=headers, data=token_params) as response:
                response.raise_for_status()
                response_data = await response.json()
            
            if "access_token" not in response_data or "refresh_token" not in response_data:
                logger.error(f"Invalid token response: {response_data}")
                raise BackendError("Failed to exchange code for tokens: Invalid response")
            
            self._access_token = response_data["access_token"]
            self._refresh_token = response_data["refresh_token"]
            self._save_lats()
            
            logger.info("Successfully exchanged code for tokens")
            return self._access_token, self._refresh_token
        except aiohttp.ClientError as e:
            logger.exception(f"Network error while exchanging code for tokens: {str(e)}")
            raise NetworkError("Failed to exchange code for tokens due to network error")
        except Exception as e:
            logger.exception(f"Unexpected error while exchanging code for tokens: {str(e)}")
            raise BackendError("Unexpected error while exchanging code for tokens")

    async def _refresh_access_token(self):
        if self._refresh_token is None:
            raise AuthenticationRequired("No refresh token available")
        
        url = "https://accounts.ea.com/connect/token"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        params = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token
        }
        try:
            async with self._session.post(url, headers=headers, data=params) as response:
                response.raise_for_status()
                data = await response.json()
            
            if "access_token" in data and "refresh_token" in data:
                self._access_token = data["access_token"]
                self._refresh_token = data["refresh_token"]
                self._save_lats()
            else:
                raise BackendError("Failed to refresh token: Invalid response")
        except aiohttp.ClientError as e:
            logger.warning(f"Network error while refreshing token: {str(e)}")
            raise NetworkError("Failed to refresh token due to network error")
        except Exception as e:
            logger.exception(f"Failed to refresh token: {str(e)}")
            self._access_token = None
            self._refresh_token = None
            raise AccessDenied("Failed to refresh token")

    async def _get_access_token(self):
        url = "https://accounts.ea.com/connect/auth"
        params = {
            "client_id": self._client_id,
            "display": "junoWeb/login",
            "response_type": "code",
            "redirectUri": "nucleus:rest"
        }
        async with self._session.get(url, params=params, allow_redirects=False) as response:
            if "Location" in response.headers:
                location = response.headers["Location"]
                if "code=" in location:
                    return location.split("code=")[1].split("&")[0]
                elif "error=login_required" in location:
                    self._log_session_details()
                    raise AuthenticationRequired("Error obtaining authorization code. Must reauthenticate.")
            self._save_lats()
            raise BackendError("Unexpected response during authorization")

    def _save_lats(self):
        if self._save_lats_callback is not None:
            self._last_access_token_success = int(time.time())
            self._save_lats_callback(self._last_access_token_success)

    def set_save_lats_callback(self, callback):
        self._save_lats_callback = callback

    def load_lats_from_cache(self, value: Optional[str]):
        self._last_access_token_success = int(value) if value else None

    def _log_session_details(self):
        try:
            utag_main_cookie = next(filter(lambda c: c.key == 'utag_main', self._cookie_jar))
            utag_main = {i.split(':')[0]: i.split(':')[1] for i in utag_main_cookie.value.split('$')}
            logger.info('now: %s st: %s ses_id: %s lats: %s',
                str(int(time.time())),
                utag_main['_st'][:10],
                utag_main['ses_id'][:10],
                str(self._last_access_token_success)
            )
        except Exception as e:
            logger.warning('Failed to get session duration: %s', repr(e))