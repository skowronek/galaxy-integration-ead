import asyncio
import urllib.parse
import base64
import os
import pathlib
import json
import logging
import platform
import subprocess
import sys
import time
import webbrowser
from functools import partial
from typing import Any, Callable, Dict, List, NewType, Optional, AsyncGenerator, NamedTuple, Set, Iterable
import winreg

from galaxy.api.consts import LicenseType, Platform
from galaxy.api.errors import (
    AccessDenied, AuthenticationRequired, BackendError, InvalidCredentials, UnknownBackendResponse, UnknownError
)
from galaxy.api.plugin import create_and_run_plugin, Plugin
from galaxy.api.types import (
    Achievement, Authentication, FriendInfo, Game, GameTime, LicenseInfo, LocalGame, LocalGameState,
    NextStep, Subscription, SubscriptionGame
)

from backend import MasterTitleId, OfferId, EABackendClient, Timestamp, AchievementSet, Json
from http_client import AuthenticatedHttpClient
from lgames_manifests import (
    get_install_location_rkeyxml, get_state_changes, parse_total_size, process_iter,
    discover_games_from_offer_cache, game_is_running_by_path, find_game_executables
)
from pcsign_hash import PCSign, PCSignVersion
from uri_scheme_handler import is_uri_handler_installed
from version import __version__
import re


logger = logging.getLogger(__name__)

def is_windows():
    return platform.system().lower() == "windows"

LOCAL_GAMES_CACHE_VALID_PERIOD = 3600  # 1 hour
def regex_pattern(regex):
    return ".*" + re.escape(regex) + ".*"

MultiplayerId = NewType("MultiplayerId", str)
GameId = NewType("GameId", str)  # eg. Origin.OFR:12345 or Origin.OFR:12345@epic
GameSlug = NewType("GameSlug", str)  # eg. "battlefield-1"
# but since EA Desktop has changed their launch format, we need to use the contentId to launch the games (eg: "1026023" for Battlefield 1)

class AchievementsImportContext(NamedTuple):
    owned_games: Dict[GameSlug, AchievementSet]
    achievements: Dict[AchievementSet, List[Achievement]]


class GameLibrarySettingsContext(NamedTuple):
    favorite: Set[OfferId]
    hidden: Set[OfferId]


class EAPlugin(Plugin):
    def __init__(self, reader, writer, token):
        super().__init__(Platform.Origin, __version__, reader, writer, token)
        self._load_stored_credentials()
        self._user_id = None
        self._persona_id = None
        self._access_token = None
        self._refresh_token = None

        self._http_client = AuthenticatedHttpClient()
        self._backend_client = EABackendClient(self._http_client)
        self._persistent_cache_updated = False

        self._local_games = {}
        self._local_games_last_update = 0
        self._local_games_update_in_progress = False

    @property
    def _game_time_cache(self) -> Dict[OfferId, GameTime]:
        return self.persistent_cache.setdefault("game_time", {})

    @property
    def _offer_id_cache(self) -> Dict[OfferId, Json]:
        return self.persistent_cache.setdefault("offers", {})

    def _load_stored_credentials(self):
        creds = self.persistent_cache.get('credentials')
        if creds:
            self._access_token = creds.get('access_token')
            self._refresh_token = creds.get('refresh_token')
    
    
    def _update_local_games(self):
        """
        Updated method to use improved game discovery through offer cache
        and better process detection with cross-platform support
        """
        local_games = []
        # Ensure we have offer cache data
        if not self._offer_id_cache:
            logger.debug("Offer cache is empty, attempting to get owned offers")
            loop = asyncio.get_event_loop()
            try:
                loop.run_until_complete(self._get_owned_offers())
            except Exception as e:
                logger.error(f"Failed to get owned offers: {str(e)}")
                
        for offer_id, game_data in self._offer_id_cache.items():
            state = LocalGameState.None_
            install_path = None
            
            # Try to get installation path - improved cross-platform support
            if "installPath" in game_data and game_data["installPath"] and os.path.exists(game_data["installPath"]):
                install_path = game_data["installPath"]
            elif platform.system() == "Windows":
                # Try to get from registry or XML manifest
                for location_key in ["installCheckOverride", "executePathOverride"]:
                    if game_data.get(location_key):
                        try:
                            location = game_data[location_key]
                            if '[' in location and ']' in location:
                                regkey_path, part = location.split(']', 1)
                                regkey_parts = regkey_path.strip('[').split("\\")
                                
                                if len(regkey_parts) >= 2:
                                    hive = getattr(winreg, regkey_parts[0])
                                    reg_path = "\\".join(regkey_parts[1:-1])
                                    reg_key = regkey_parts[-1]
                                    
                                    install_path = get_install_location_rkeyxml(hive, reg_path, reg_key)
                                    if install_path and os.path.exists(install_path):
                                        break
                            elif os.path.exists(location):
                                install_path = location
                                break
                        except Exception as e:
                            logger.debug(f"Failed to process install location {location}: {e}")
            else:
                # For macOS and other platforms
                for key in ["installLocation", "path", "installCheckOverride", "executePathOverride"]:
                    if key in game_data and game_data[key] and os.path.exists(game_data[key]):
                        install_path = game_data[key]
                        break
            
            if install_path:
                state = LocalGameState.Installed
                
                # Check if the game is running - improved detection logic
                try:
                    if game_is_running_by_path(install_path):
                        state |= LocalGameState.Running
                        logger.info(f"Game running detected for {offer_id} at {install_path}")
                except Exception as e:
                    logger.error(f"Failed to check if game is running: {e}")
                    
            local_games.append(LocalGame(offer_id, state))
        
        return local_games
    
    def _local_game_status(self):
        '''
        returns list of changed games (added, removed, or changed)
        updated local_games property
        '''
        new_local_games = self._update_local_games()
        notify_list = get_state_changes(self._local_games, new_local_games)
        self._local_games = new_local_games

        return self._local_games, notify_list
        
    async def shutdown(self):
        await self._http_client.close()

    def tick(self):
        self.handle_local_game_update_notifications()

    async def _check_authenticated(self):
        if not self._access_token or not self._refresh_token:
            raise AuthenticationRequired()

    async def authenticate(self, stored_credentials=None):
        if stored_credentials:
            self._refresh_token = stored_credentials.get('refresh_token')
            if self._refresh_token:
                try:
                    # Force refresh the token every time
                    await self._force_refresh_access_token()
                    return await self._get_user_info()
                except Exception as e:
                    logging.error(f"Failed to refresh token: {e}")
                    # Clear invalid refresh token
                    self._refresh_token = None
        
        return await self._begin_auth_flow()

    async def _begin_auth_flow(self):
        pc_sign_definition = PCSign(sv=PCSignVersion.V2)
        pc_sign = pc_sign_definition.generate_pc_sign()
        params = {
            "window_title": "Login to EA Desktop",
            "window_width": 495 if is_windows() else 480,
            "window_height": 746 if is_windows() else 708,
            "start_uri": "https://accounts.ea.com/connect/auth"
                        "?response_type=code&client_id=JUNO_PC_CLIENT&display=junoClient/login"
                        "&redirect_uri=qrc:///html/login_successful.html"
                        "&locale=en_US&pc_sign={}".format(pc_sign),
            "end_uri_regex": "qrc:/html/login_successful.html.*"
        }
        script = {regex_pattern(r"juno/login?execution"): [
            r'''
                document.getElementById("rememberMe").checked = true;
            '''
        ]}
        return NextStep("web_session", params, js=script)
    
    async def _force_refresh_access_token(self):
        try:
            self._access_token, self._refresh_token = await self._http_client._refresh_access_token(self._refresh_token)
            self.store_credentials({
                'refresh_token': self._refresh_token
            })
            # Don't store access_token in persistent storage
        except Exception as e:
            logging.error(f"Failed to refresh token: {e}")
            raise AuthenticationRequired()

    def _store_tokens(self, access_token, refresh_token):
        self.store_credentials({
            "access_token": access_token,
            "refresh_token": refresh_token
        })

    async def _get_user_info(self):
        payload = self._decode_jwt_payload(self._access_token)
        self._user_id = payload['nexus']["pid"]
        user_name = payload['nexus']["psif"][0]["dis"]
        self._persona_id = payload['nexus']["psid"]
        return Authentication(self._user_id, user_name)

    def _decode_jwt_payload(self, token):
        _, payload, _ = token.split('.')
        padding = '=' * (4 - len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload + padding).decode('utf-8'))

    async def _refresh_access_token(self):
        self._access_token, self._refresh_token = await self._http_client._refresh_access_token(self._refresh_token)
        self._store_tokens(self._access_token, self._refresh_token)

    def _extract_code_from_uri(self, uri):
        parsed_uri = urllib.parse.urlparse(uri)
        query_params = urllib.parse.parse_qs(parsed_uri.query)
        
        if 'code' in query_params:
            return query_params['code'][0]
        else:
            raise AuthenticationRequired("No authorization code found in redirect URI")

    async def pass_login_credentials(self, step, credentials, cookies):
        logger.debug("Passing login credentials: step {}, credentials {}, cookies {}".format(step, credentials, cookies))
        auth_code = self._extract_code_from_uri(credentials["end_uri"])
        return await self._do_authenticate(auth_code)

    async def _do_authenticate(self, auth_code):
        try:
            logger.info("Starting authentication process")
            self._access_token, self._refresh_token = await self._http_client._exchange_code_for_token(auth_code)
            logger.info("Access token obtained")
            
            if not self._access_token:
                logger.error("Access token not set after _exchange_code_for_token")
                raise AccessDenied("No access token obtained")
            
            user_info = self._decode_jwt_payload(self._access_token)["nexus"]
            self._user_id = user_info["pid"]
            user_name = user_info["psif"][0]["dis"]
            self._persona_id = user_info["psid"]
            logger.info(f"Identity obtained: user_id={self._user_id}, user_name={user_name}, persona_id={self._persona_id}")
            
            self._store_tokens(self._access_token, self._refresh_token)
            
            return Authentication(self._user_id, user_name)
        except (AccessDenied, InvalidCredentials, AuthenticationRequired) as e:
            logger.exception(f"Failed to authenticate: {repr(e)}")
            raise InvalidCredentials()

    @staticmethod
    def _offer_id_from_game_id(game_id: GameId) -> OfferId:
        return OfferId(game_id.split('@')[0])

    async def get_owned_games(self) -> List[Game]:
        self._check_authenticated()

        owned_offers = await self._get_owned_offers()
        games = []
        for game_id, offer in owned_offers.items():
            if game_id and offer is not None:
                if "displayName" in offer or "i18n" in offer:
                    game = Game(
                        game_id,
                        offer.get("displayName") or offer["i18n"].get("displayName"),
                        None,
                        LicenseInfo(LicenseType.SinglePurchase, None)
                    )
                games.append(game)

        return games

    async def prepare_achievements_context(self, game_ids: List[GameId]) -> AchievementsImportContext:
        self._check_authenticated()
        achievement_sets: Dict[OfferId, AchievementSet] = dict()
        achievements = []
        for game_id in game_ids:
            try:
                offer = self._offer_id_from_game_id(game_id)
                achievement_set = await self._backend_client.get_achievement_set(offer, self._persona_id)
                if achievement_set is not None:
                    achievement_sets[offer] = achievement_set
                    achievements = await self._backend_client.get_achievements(offer, self._persona_id)
                else:
                    logger.debug(f"No achievements found for game {offer}")
            except TypeError as e:
                print(f"Error retrieving achievements for game {offer}: {e}")
        return AchievementsImportContext(
            owned_games=achievement_sets,
            achievements=achievements
        )

    async def get_unlocked_achievements(self, game_id: GameId, context: AchievementsImportContext) -> List[Achievement]:
        offer = self._offer_id_from_game_id(game_id)
        if offer not in context.owned_games:
            logger.warning("Game '{}' doesn't have achievements.".format(game_id))
            return []
        else:
            achievements_set = context.owned_games[offer]
            achievements = context.achievements.get(achievements_set)
            if achievements is not None:
                return achievements

            return (await self._backend_client.get_achievements(
                offer, self._persona_id
            ))[achievements_set]

    async def _get_offers(self, offer_ids: Iterable[OfferId]) -> Dict[OfferId, Json]:
        """
        Get offers from cache if exists.
        Fetch from backend if not and update cache.
        Using the new batched approach for better performance.
        """
        offers = {}
        missing_offers = []
        
        # First check what we have in cache
        for offer_id in offer_ids:
            offer = self._offer_id_cache.get(offer_id, None)
            if offer is not None:
                offers[offer_id] = offer
            else:
                missing_offers.append(offer_id)

        # If we have missing offers, fetch them in batches
        if missing_offers:
            try:
                # Use the new batch method if there are multiple missing offers
                if len(missing_offers) > 1:
                    batched_offers = await self._backend_client.get_offers_batch(missing_offers)
                    for offer_id, offer_data in batched_offers.items():
                        if offer_data[0] and offer_data[1]:
                            # Structure the data consistently
                            if "executePathOverride" in offer_data[0] and offer_data[0]["executePathOverride"] != "":
                                offer_data[0]["gameSlug"] = offer_data[1]["gameSlug"]
                                offers[offer_id] = offer_data[0]
                                self._offer_id_cache[offer_id] = offer_data[0]
                else:
                    # For a single offer, use the original method
                    for offer_id in missing_offers:
                        try:
                            offer_data = await self._backend_client.get_offer(offer_id)
                            if offer_data[0] and offer_data[1]:
                                if "executePathOverride" in offer_data[0] and offer_data[0]["executePathOverride"] != "":
                                    offer_id = offer_data[1]["originOfferId"]
                                    offer_data[0]["gameSlug"] = offer_data[1]["gameSlug"]
                                    offers[offer_id] = offer_data[0]
                                    self._offer_id_cache[offer_id] = offer_data[0]
                        except Exception as e:
                            logger.error(f"Error fetching offer {offer_id}: {e}")
                
                # Save the updated cache
                self.push_cache()
            except Exception as e:
                logger.error(f"Error fetching offers: {e}")

        return offers
    
    async def _get_owned_offers(self) -> Dict[GameId, Json]:
        await self._check_authenticated()

        def get_game_id(entitlement: Json) -> GameId:
            offer_id = entitlement["originOfferId"]
            external_type = entitlement.get("product", {}).get("gameProductUser", {}).get("ownershipMethods", [None])[0]
            return GameId(f"{offer_id}@{external_type.lower()}" if external_type in ["STEAM", "EPIC"] else offer_id)

        def is_valid_game(entitlement: Json) -> bool:
            if not entitlement.get("product"):
                return False
            game_type = entitlement["product"].get("baseItem", {}).get("gameType")
            # Include BASE_GAME and EXPANSION types, exclude DLC and VIRTUAL_CURRENCY
            return game_type in ["BASE_GAME", "EXPANSION"]

        entitlement_data = await self._backend_client.get_entitlements()
        valid_entitlements = [x for x in entitlement_data if is_valid_game(x)]
        basegame_offers = await self._get_offers([x["originOfferId"] for x in valid_entitlements])

        return {
            get_game_id(ent): basegame_offers[ent["originOfferId"]]
            for ent in valid_entitlements
            if ent["originOfferId"] in basegame_offers
        }

    async def get_subscriptions(self) -> List[Subscription]:
        self._check_authenticated()
        return await self._backend_client.get_subscriptions()

    async def prepare_subscription_games_context(self, subscription_names: List[str]) -> Any:
        self._check_authenticated()
        return {
            'EA Play': 'standard',
            'EA Play Pro': 'premium'
        }

    async def get_subscription_games(self, subscription_name: str, context: Dict[str, str]
    ) -> AsyncGenerator[List[SubscriptionGame], None]:
        try:
            tier = context[subscription_name]
        except KeyError:
            raise UnknownError(f'Unknown subscription name {subscription_name}!')
        yield await self._backend_client.get_games_in_subscription(tier)

    async def _get_game_times_for_master_title(self, game_id: GameId, game_slug: GameSlug, lastplayed_time: Optional[Timestamp]) -> GameTime:
        """
        :param game_id - to get from cache
        :param game_slug - to fetch from backend
        :param lastplayed_time - to decide on cache freshness
        """
        def get_cached_game_times(_game_id: GameId, _lastplayed_time: Optional[Timestamp]) -> Optional[GameTime]:
            """"returns None if a new entry should be retrieved"""
            if _lastplayed_time is None:
                # double-check if 'lastplayed_time' is unknown (maybe it was just too long ago)
                return None

            _cached_game_time: GameTime = self._game_time_cache.get(_game_id)
            if _cached_game_time is None or _cached_game_time.last_played_time is None:
                # played time unknown yet
                return None
            if _lastplayed_time > _cached_game_time.last_played_time:
                # newer played time available
                return None
            return _cached_game_time

        cached_game_time: Optional[GameTime] = get_cached_game_times(game_id, lastplayed_time)
        if cached_game_time is not None:
            return cached_game_time

        total_play_time, last_played_time = await self._backend_client.get_game_time(game_slug)
        game_time: GameTime = GameTime(game_id, total_play_time, last_played_time)
        self._game_time_cache[game_id] = game_time
        self._persistent_cache_updated = True
        return game_time

    async def prepare_game_times_context(self, game_ids: List[GameId]) -> Any:
        """
        Prepare game times context using batched requests for improved performance
        """
        await self._check_authenticated()
        
        # Extract offer IDs and game slugs from the game IDs
        offer_ids = [self._offer_id_from_game_id(game_id) for game_id in game_ids]
        
        # Ensure we have all offer information in cache
        missing_offers = [offer_id for offer_id in offer_ids if offer_id not in self._offer_id_cache]
        if missing_offers:
            await self._get_offers(missing_offers)
            
        # Extract game slugs from cache using dictionary comprehension (plus efficace)
        game_slugs = []
        for offer_id in offer_ids:
            offer = self._offer_id_cache.get(offer_id)
            if offer:
                if "gameSlug" in offer:
                    game_slugs.append(GameSlug(offer["gameSlug"]))
                elif "gameNameFacetKey" in offer:
                    game_slugs.append(GameSlug(offer["gameNameFacetKey"]))
        
        # Utiliser la nouvelle méthode batch pour les temps de jeu
        if len(game_slugs) > 1:
            return await self._backend_client.get_lastplayed_games(game_slugs)
        elif len(game_slugs) == 1:
            # Pour un seul jeu
            result = await self._backend_client.get_lastplayed_games(game_slugs)
            return result
        else:
            return {}

    async def get_game_time(self, game_id: GameId, last_played_games: Any) -> GameTime:
        offer_id = self._offer_id_from_game_id(game_id)
        try:
            offer = self._offer_id_cache.get(offer_id)
            if offer is None:
                logger.exception("Internal cache out of sync")
                raise UnknownError()
            if "gameSlug" in offer:
                game_slug = GameSlug(offer["gameSlug"])
            else:
                # Specific case in which offer data's in the other format
                game_slug = GameSlug(offer["gameNameFacetKey"])

            return await self._get_game_times_for_master_title(
                game_id,
                game_slug,
                last_played_games.get(game_slug)
            )

        except KeyError as e:
            logger.exception("Failed to import game times %s", repr(e))
            raise UnknownBackendResponse()

    def game_times_import_complete(self):
        if self._persistent_cache_updated:
            self.push_cache()
            self._persistent_cache_updated = False

    async def get_friends(self):
        self._check_authenticated()

        return [
            FriendInfo(user_id=str(user_id), user_name=str(user_name))
            for user_id, user_name in (await self._backend_client.get_friends()).items()
        ]

    @staticmethod
    def _open_uri(uri):
        logger.info("Opening {}".format(uri))
        webbrowser.open(uri)
    
    async def launch_game(self, game_id: GameId):
        offer_id = self._offer_id_from_game_id(game_id)
        offer = self._offer_id_cache.get(offer_id)
        if offer is None:
            logger.exception("Internal cache out of sync")
            raise UnknownError()
        
        master_title_id: MasterTitleId = offer["contentId"]
        
        # Platform-specific launching
        if platform.system() == "Windows":
            if is_uri_handler_installed("origin2"):
                uri = "origin2://game/launch?offerIds={}&autoDownload=1".format(master_title_id)
            else:
                uri = "https://www.ea.com/ea-app"
            self._open_uri(uri)
        elif platform.system() == "Darwin":
            # Try to find installed EA Desktop app on macOS
            app_paths = [
                "/Applications/EA Desktop.app",
                os.path.expanduser("~/Applications/EA Desktop.app")
            ]
            
            app_found = False
            for app_path in app_paths:
                if os.path.exists(app_path):
                    try:
                        subprocess.Popen(["open", "-a", app_path, "--args", f"game/launch?offerIds={master_title_id}&autoDownload=1"])
                        app_found = True
                        break
                    except Exception as e:
                        logger.error(f"Failed to launch EA Desktop app: {str(e)}")
            
            if not app_found:
                # Fallback to web
                uri = "https://www.ea.com/ea-app"
                self._open_uri(uri)
        else:
            # Fallback for other platforms
            uri = "https://www.ea.com/ea-app"
            self._open_uri(uri)
        
        # Check for running state after launch with retries
        asyncio.create_task(self._check_game_running_after_launch(game_id, offer))

    async def _check_game_running_after_launch(self, game_id: GameId, offer: dict, max_attempts: int = 10, delay: float = 3.0):
        """Check if the game starts running after launch and update its status if needed.
        
        Args:
            game_id: The ID of the launched game
            offer: The game's offer data containing install path info
            max_attempts: Maximum number of retry attempts
            delay: Delay between retry attempts in seconds
        """
        logger.info(f"Starting post-launch state monitoring for {game_id}")
        
        # Wait a moment before starting checks to allow the game to begin launching
        await asyncio.sleep(delay)
        
        # Look for install path using more comprehensive approach
        install_path = None
        
        # First try direct paths
        for key in ["installPath", "installLocation", "path"]:
            if key in offer and offer[key] and os.path.exists(offer[key]):
                install_path = offer[key]
                logger.debug(f"Found direct install path for {game_id}: {install_path}")
                break
        
        # If no direct path found and we're on Windows, try registry paths
        if not install_path and platform.system() == "Windows":
            for location_key in ["installCheckOverride", "executePathOverride"]:
                if location_key in offer and offer[location_key]:
                    try:
                        location = offer[location_key]
                        if '[' in location and ']' in location:
                            regkey_path, part = location.split(']', 1)
                            regkey_parts = regkey_path.strip('[').split("\\")
                            
                            if len(regkey_parts) >= 2:
                                hive = getattr(winreg, regkey_parts[0])
                                reg_path = "\\".join(regkey_parts[1:-1])
                                reg_key = regkey_parts[-1]
                                
                                path = get_install_location_rkeyxml(hive, reg_path, reg_key)
                                if path and os.path.exists(path):
                                    install_path = path
                                    logger.debug(f"Found registry install path for {game_id}: {install_path}")
                                    break
                        elif os.path.exists(location):
                            install_path = location
                            logger.debug(f"Found location path for {game_id}: {install_path}")
                            break
                    except Exception as e:
                        logger.debug(f"Failed to process install location {location} for {game_id}: {e}")
        
        # Fall back to checking local manifests
        if not install_path:
            try:
                # Get installation info from locally installed games cache if available
                local_games, _ = self._local_game_status()
                for local_game in local_games:
                    if local_game.game_id == game_id and local_game.local_game_state & LocalGameState.Installed:
                        # Game is installed according to our detection, try to use that info
                        offer_id = self._offer_id_from_game_id(game_id)
                        for cached_offer_id, cached_offer in self._offer_id_cache.items():
                            if cached_offer_id == offer_id and "installPath" in cached_offer and os.path.exists(cached_offer["installPath"]):
                                install_path = cached_offer["installPath"]
                                logger.debug(f"Found install path from cache for {game_id}: {install_path}")
                                break
            except Exception as e:
                logger.error(f"Error finding install path from local game status for {game_id}: {e}")
                
        if not install_path:
            logger.error(f"Could not find valid install path for {game_id}")
            return
            
        # Check periodically if the game starts running
        attempt = 0
        while attempt < max_attempts:
            try:
                logger.debug(f"Checking running state for {game_id} attempt {attempt+1}/{max_attempts}")
                
                if game_is_running_by_path(install_path):
                    logger.info(f"Game {game_id} detected as running after launch")
                    # Update game state to Running
                    self.update_local_game_status(LocalGame(game_id, LocalGameState.Installed | LocalGameState.Running))
                    return
            except Exception as e:
                logger.error(f"Error checking if game is running: {e}")
                
            attempt += 1
            await asyncio.sleep(delay)
            
        logger.warning(f"Failed to detect running state for {game_id} after {max_attempts} attempts")

    async def install_game(self, game_id: GameId):
        def is_subscription_game(game_id: GameId) -> bool:
            return game_id.endswith('subscription')
        def is_offer_missing_from_user_library(offer_id: OfferId):
            return offer_id not in self._offer_id_cache
        
        async def get_subscription_game_store_uri(offer_id):
            try:
                offer = await self._backend_client.get_offer(offer_id)
                return "https://www.ea.com/games/{}".format(offer["gdpPath"])
            except (KeyError, UnknownError, BackendError, UnknownBackendResponse):
                return "https://www.ea.com/ea-play/games"
                
        offer_id = self._offer_id_from_game_id(game_id)
        
        if is_subscription_game(game_id) and is_offer_missing_from_user_library(offer_id):
            uri = await get_subscription_game_store_uri(offer_id)
        elif platform.system() == "Windows" and is_uri_handler_installed("origin2"):
            offer = self._offer_id_cache.get(offer_id)
            if offer is None:
                logger.exception("Internal cache out of sync")
                raise UnknownError()
            master_title_id: MasterTitleId = offer["contentId"]
            uri = "origin2://game/launch?offerIds={}".format(master_title_id)
        elif platform.system() == "Darwin":
            # Try to find installed EA Desktop app on macOS
            app_paths = [
                "/Applications/EA Desktop.app",
                os.path.expanduser("~/Applications/EA Desktop.app")
            ]
            
            app_found = False
            for app_path in app_paths:
                if os.path.exists(app_path):
                    try:
                        offer = self._offer_id_cache.get(offer_id)
                        if offer is None:
                            logger.exception("Internal cache out of sync")
                            raise UnknownError()
                        master_title_id = offer["contentId"]
                        subprocess.Popen(["open", "-a", app_path, "--args", f"game/launch?offerIds={master_title_id}&autoDownload=1"])
                        return  # Exit early if we successfully launched
                    except Exception as e:
                        logger.error(f"Failed to launch EA Desktop app: {str(e)}")
            
            # Fallback to web if app not found
            uri = "https://www.ea.com/ea-app"
        else:
            # Fallback for all other cases
            uri = "https://www.ea.com/ea-app"
            
        self._open_uri(uri)

    # Platform-specific uninstall
    if platform.system() == "Windows":
        async def uninstall_game(self, game_id: GameId):
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, partial(subprocess.run, ["control", "appwiz.cpl"]))
    elif platform.system() == "Darwin":
        async def uninstall_game(self, game_id: GameId):
            # On macOS, attempt to find the game's application bundle and move it to trash
            game_path = None
            offer_id = self._offer_id_from_game_id(game_id)
            offer = self._offer_id_cache.get(offer_id)
            
            if offer:
                # Look for install location
                for key in ["installPath", "installLocation", "path", "installCheckOverride", "executePathOverride"]:
                    if key in offer and offer[key] and os.path.exists(offer[key]):
                        game_path = offer[key]
                        break
            
            if game_path:
                # Try to find a .app bundle in the game path
                app_bundles = []
                for root, dirs, _ in os.walk(game_path):
                    for dir_name in dirs:
                        if dir_name.endswith('.app'):
                            app_bundles.append(os.path.join(root, dir_name))
                
                if app_bundles:
                    # Try to move the first .app bundle to trash
                    try:
                        from AppKit import NSWorkspace
                        workspace = NSWorkspace.sharedWorkspace()
                        url = NSURL.fileURLWithPath_(app_bundles[0])
                        workspace.recycleURLs_completionHandler_([url], None)
                        return
                    except (ImportError, NameError):
                        pass
                        
                    # Fallback to osascript
                    try:
                        subprocess.run(["osascript", "-e", f'tell application "Finder" to move POSIX file "{app_bundles[0]}" to trash'])
                        return
                    except Exception as e:
                        logger.error(f"Failed to move app to trash: {str(e)}")
            
            # If we couldn't uninstall directly, open EA Desktop app
            app_paths = [
                "/Applications/EA Desktop.app",
                os.path.expanduser("~/Applications/EA Desktop.app")
            ]
            
            for app_path in app_paths:
                if os.path.exists(app_path):
                    subprocess.Popen(["open", "-a", app_path])
                    return
            
            # Last resort: open EA website
            self._open_uri("https://www.ea.com/ea-app")

    async def shutdown_platform_client(self) -> None:
        if platform.system() == "Windows":
            self._open_uri("origin://quit")
        elif platform.system() == "Darwin":
            # Try to find EA Desktop process and quit it
            try:
                subprocess.run(["pkill", "-x", "EA Desktop"], check=False)
            except Exception:
                pass

    def _store_cookies(self, cookies):
        credentials = {
            "cookies": cookies
        }
        self.store_credentials(credentials)

    def _update_stored_cookies(self, morsels):
        cookies = {}
        for morsel in morsels:
            cookies[morsel.key] = morsel.value
        self._store_cookies(cookies)

    async def get_local_games(self) -> List[LocalGame]:
        await self._check_authenticated()
        if self._local_games_update_in_progress:
            logger.debug("Local games are being updated, returning cached values")
            return self._local_games.local_games

        loop = asyncio.get_running_loop()
        try:
            self._local_games_update_in_progress = True
            local_games, _ = await loop.run_in_executor(None, partial(self._local_game_status))
            self._local_games_last_update = time.time()
        finally:
            self._local_games_update_in_progress = False
        return local_games

    def handle_local_game_update_notifications(self):
        async def notify_local_games_changed():
            notify_list = []
            try:
                self._local_games_update_in_progress = True
                _, notify_list = await loop.run_in_executor(None, partial(self._local_game_status))
                self._local_games_last_update = time.time()
            finally:
                self._local_games_update_in_progress = False

            for local_games_notify in notify_list:
                self.update_local_game_status(local_games_notify)

        # don't overlap update operations
        if self._local_games_update_in_progress:
            logger.debug("Local games are being updated, skipping cache update")
            return

        if time.time() - self._local_games_last_update < LOCAL_GAMES_CACHE_VALID_PERIOD:
            logger.debug("Local games cache is fresh enough")
            return

        loop = asyncio.get_running_loop()
        asyncio.create_task(notify_local_games_changed())

    async def prepare_local_size_context(self, game_ids: List[str]) -> Dict[str, Optional[pathlib.Path]]:
        game_id_crc_map: Dict[str, Optional[pathlib.Path]] = {}
        
        for game_id in game_ids:
            game = self._offer_id_cache.get(self._offer_id_from_game_id(game_id))
            if not game:
                game_id_crc_map[game_id] = None
                continue
                
            install_path = None
            
            # Look for various location indicators in the game data
            for key in ["installPath", "installLocation", "path", "installCheckOverride", "executePathOverride"]:
                if key in game and game[key]:
                    # If it's a registry path and we're on Windows
                    if platform.system() == "Windows" and key in ["installCheckOverride", "executePathOverride"] and '[' in game[key] and ']' in game[key]:
                        try:
                            regkey_path, part = game[key].split(']')
                            regkey_parts = regkey_path.strip('[').split("\\")
                            
                            if len(regkey_parts) >= 2:
                                hive = getattr(winreg, regkey_parts[0])
                                reg_path = "\\".join(regkey_parts[1:-1])
                                reg_key = regkey_parts[-1]
                                
                                path = get_install_location_rkeyxml(hive, reg_path, reg_key)
                                if path and os.path.exists(path):
                                    install_path = path
                                    break
                        except Exception as e:
                            logger.debug(f"Failed to process registry path {game[key]}: {e}")
                    # Direct path
                    elif os.path.exists(game[key]):
                        install_path = game[key]
                        break
            
            if install_path and os.path.exists(install_path):
                game_id_crc_map[game_id] = pathlib.Path(install_path)
            else:
                game_id_crc_map[game_id] = None
        
        return game_id_crc_map

    async def get_local_size(self, game_id: GameId, context: Dict[str, pathlib.PurePath]) -> Optional[int]:
        try:
            return parse_total_size(context[game_id])
        except (FileNotFoundError, OSError) as e:
            logger.debug(f"Failed to get size for {game_id}: {str(e)}")
            return None
        except KeyError:
            raise UnknownError("Manifest not found")

    def handshake_complete(self):
        def game_time_decoder(cache: Dict) -> Dict[OfferId, GameTime]:

            # after offerId -> gameId migration
            outdated_keys = [key.split('@')[0] for key in cache if "@" in key]
            for i in outdated_keys:
                cache.pop(i, None)

            return {
                game_id: GameTime(entry["game_id"], entry["time_played"], entry.get("last_played_time"))
                for game_id, entry in cache.items()
                if entry and game_id
            }

        def safe_decode(_cache: Dict, _key: str, _decoder: Callable):
            if not _cache:
                return {}
            if _decoder is None:
                _decoder = lambda x: x

            try:
                return _decoder(json.loads(_cache))
            except Exception:
                logger.exception("Failed to decode persistent '%s' cache", _key)
                return {}

        # parse caches
        cache_decoders = {
            "offers": None,
            "game_time": game_time_decoder,
        }

        for key, decoder in cache_decoders.items():
            self.persistent_cache[key] = safe_decode(self.persistent_cache.get(key), key, decoder)

            self._http_client.load_lats_from_cache(self.persistent_cache.get('lats'))
            self._http_client.set_save_lats_callback(self._save_lats)

    def _save_lats(self, lats: int):
        self.persistent_cache['lats'] = str(lats)
        self.push_cache()

def main():
    create_and_run_plugin(EAPlugin, sys.argv)

if __name__ == "__main__":
    main()
