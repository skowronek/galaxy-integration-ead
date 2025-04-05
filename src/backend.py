import json
import logging
import asyncio
from collections import namedtuple
from datetime import datetime
from typing import Dict, List, NewType, Optional, Any, Tuple, Set, Iterable
import time

from galaxy.api.errors import UnknownBackendResponse
from galaxy.api.types import Achievement, SubscriptionGame, Subscription

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

MasterTitleId = NewType("MasterTitleId", str)
AchievementSet = NewType("AchievementSet", str)
OfferId = NewType("OfferId", str)
Timestamp = NewType("Timestamp", int)
GameSlug = NewType("GameSlug", str)
Json = Dict[str, Any]

SubscriptionDetails = namedtuple('SubscriptionDetails', ['tier', 'end_time'])

API_URL = "https://service-aggregation-layer.juno.ea.com/graphql"

# Cache configuration
CACHE_EXPIRY = 300  # 5 minutes cache expiry for GraphQL responses

class EABackendClient:
    def __init__(self, http_client):
        self._http_client = http_client
        self._response_cache = {}
        self._cache_timestamps = {}
        self._cache_lock = asyncio.Lock()

    async def _make_graphql_request(self, query: str, variables: Optional[Dict] = None, force_refresh: bool = False) -> Json:
        """Make a GraphQL request with caching to reduce API calls"""
        cache_key = f"{query}:{json.dumps(variables) if variables else ''}"
        
        # Return from cache if entry exists and not expired or force_refresh
        if not force_refresh:
            async with self._cache_lock:
                if cache_key in self._response_cache:
                    # Check if cache is still valid
                    if time.time() - self._cache_timestamps[cache_key] < CACHE_EXPIRY:
                        logger.debug("Using cached response for query")
                        return self._response_cache[cache_key]
        
        # Make the actual request
        url = f"{API_URL}?query={query}"
        if variables:
            url += f"&variables={json.dumps(variables)}"
            
        response = await self._http_client.get(url.replace(' ', '%20').replace('+', '%20'))
        
        try:
            data = response['data']
            # Update cache
            async with self._cache_lock:
                self._response_cache[cache_key] = data
                self._cache_timestamps[cache_key] = time.time()
            return data
        except (ValueError, KeyError) as e:
            logger.exception(f"Cannot parse backend response: {e}")
            raise UnknownBackendResponse()

    async def get_entitlements(self) -> List[Json]:
        query = """
        query {
            me {
                ownedGameProducts(locale: "en", entitlementEnabled: true, storefronts: [EA, STEAM, EPIC], type: [DIGITAL_FULL_GAME, PACKAGED_FULL_GAME], platforms: [PC], paging: {limit: 9999}) {
                    items {
                        originOfferId
                        product {
                            gameSlug
                            baseItem {
                                gameType
                            }
                            gameProductUser {
                                ownershipMethods
                                entitlementId
                            }
                        }
                    }
                }
            }
        }
        """
        data = await self._make_graphql_request(query)
        items = data['me']['ownedGameProducts']['items']
        return [item for item in items if item['product'] is not None]

    async def get_offer(self, offer_id: OfferId) -> Json:
        query = """
        query($offerId: String!) {
            legacyOffers(offerIds: [$offerId], locale: "en") {
                offerId: id
                contentId
                basePlatform
                primaryMasterTitleId
                mdmTitleIds
                achievementSetOverride
                multiplayerId
                installCheckOverride
                executePathOverride
                displayName
                displayType
                metadataInstallLocation
                softwarePlatform
                softwareId
            }
            gameProducts(offerIds: [$offerId], locale: "en") {
                items {
                    name
                    originOfferId
                    baseItem {
                        title
                    }
                    gameSlug
                }
            }
        }
        """
        data = await self._make_graphql_request(query, {"offerId": str(offer_id)})
        return data['legacyOffers'][0], data['gameProducts']['items'][0]

    async def get_offers_batch(self, offer_ids: Iterable[OfferId]) -> Dict[OfferId, Json]:
        """Batch fetch multiple offers to reduce API calls"""
        # GraphQL doesn't easily support variable-length arrays in variables
        # So we'll chunk the requests
        MAX_BATCH_SIZE = 10
        results = {}
        offer_ids_list = list(offer_ids)
        
        # Process in chunks to avoid overwhelming the API
        for i in range(0, len(offer_ids_list), MAX_BATCH_SIZE):
            chunk = offer_ids_list[i:i+MAX_BATCH_SIZE]
            chunk_str = ", ".join([f'"{oid}"' for oid in chunk])
            
            query = f"""
            query {{
                legacyOffers(offerIds: [{chunk_str}], locale: "en") {{
                    offerId: id
                    contentId
                    basePlatform
                    primaryMasterTitleId
                    mdmTitleIds
                    achievementSetOverride
                    multiplayerId
                    installCheckOverride
                    executePathOverride
                    displayName
                    displayType
                    metadataInstallLocation
                    softwarePlatform
                    softwareId
                }}
                gameProducts(offerIds: [{chunk_str}], locale: "en") {{
                    items {{
                        name
                        originOfferId
                        baseItem {{
                            title
                        }}
                        gameSlug
                    }}
                }}
            }}
            """
            
            try:
                data = await self._make_graphql_request(query)
                legacy_offers = data['legacyOffers']
                game_products = data['gameProducts']['items']
                
                # Map the responses back to their offer IDs
                for legacy, product in zip(legacy_offers, game_products):
                    if legacy and product:
                        results[legacy['offerId']] = (legacy, product)
            except Exception as e:
                logger.error(f"Error batch fetching offers: {e}")
                # Continue with other chunks even if one fails
        
        return results

    async def _fetch_achievements(self, offer: OfferId, persona: str, show_hidden: bool = False) -> Json:
        query = """
        query($offerId: String!, $playerPsd: String!, $showHidden: Boolean!) {
            achievements(offerId: $offerId, playerPsd: $playerPsd, showHidden: $showHidden) {
                id
                achievements {
                    id
                    name
                    awardCount
                    date
                }
            }
        }
        """
        data = await self._make_graphql_request(query, {"offerId": str(offer), "playerPsd": str(persona), "showHidden": show_hidden})
        return data

    async def get_achievements(self, offer: OfferId, persona: str) -> Dict[str, List[Achievement]]:
        data = await self._fetch_achievements(offer, persona, show_hidden=True)
        
        def parse_achievements(json_data: Dict) -> List[Achievement]:
            achievements = []
            for achievement in json_data["achievements"]:
                if achievement["awardCount"] == 1:
                    date_obj = datetime.strptime(achievement["date"], "%Y-%m-%dT%H:%M:%S.%fZ")
                    unix_timestamp = int(date_obj.timestamp())
                    achievement_data = Achievement(
                        achievement_id=achievement["id"],
                        achievement_name=achievement["name"],
                        unlock_time=unix_timestamp
                    )
                    achievements.append(achievement_data)
            return achievements
            
        achievement_sets = {}
        for achievement_set in data["achievements"]:
            achievements = parse_achievements(achievement_set)
            achievement_sets[achievement_set["id"]] = achievements
            
        return achievement_sets

    async def get_achievement_set(self, offer_id: OfferId, persona_id: str) -> Optional[str]:
        data = await self._fetch_achievements(offer_id, persona_id, show_hidden=False)
        
        achievements = data["achievements"]
        return achievements[0]["id"] if achievements and "id" in achievements[0] else None

    async def get_game_time(self, game_slug: GameSlug) -> Tuple[int, Optional[int]]:
        query = """
        query($gameSlug: [String!]!) {
            me {
                recentGames(gameSlugs: $gameSlug) {
                    items {
                        lastSessionEndDate
                        totalPlayTimeSeconds
                    }
                }
            }
        }
        """
        data = await self._make_graphql_request(query, {"gameSlug": game_slug})
        items = data['me']['recentGames']['items']
        if not items:
            return 0, None
            
        total_play_time = round(int(items[0]['totalPlayTimeSeconds']) / 60)
        last_played_time = self._parse_timestamp(items[0]['lastSessionEndDate'])
        return total_play_time, last_played_time

    async def get_game_times_batch(self, game_slugs: List[GameSlug]) -> Dict[GameSlug, Tuple[int, Optional[int]]]:
        """Batch fetch game times for multiple games to reduce API calls"""
        if not game_slugs:
            return {}
            
        MAX_BATCH_SIZE = 20  # Limite optimale pour l'API EA
        results = {}
        
        # Filtrer les doublons pour réduire le nombre d'appels
        unique_slugs = list(set(game_slugs))
        
        # Process in chunks
        for i in range(0, len(unique_slugs), MAX_BATCH_SIZE):
            chunk = unique_slugs[i:i+MAX_BATCH_SIZE]
            query = """
            query($gameSlugs: [String!]!) {
                me {
                    recentGames(gameSlugs: $gameSlugs) {
                        items {
                            gameSlug
                            lastSessionEndDate
                            totalPlayTimeSeconds
                        }
                    }
                }
            }
            """
            
            try:
                data = await self._make_graphql_request(query, {"gameSlugs": chunk})
                items = data['me']['recentGames']['items']
                
                # Optimisation: utiliser un dictionnaire pour les données des jeux
                game_data = {item['gameSlug']: item for item in items}
                
                for slug in chunk:
                    if slug in game_data:
                        item = game_data[slug]
                        total_play_time = round(int(item['totalPlayTimeSeconds']) / 60)
                        last_played_time = self._parse_timestamp(item['lastSessionEndDate'])
                        results[slug] = (total_play_time, last_played_time)
                    else:
                        # Jeu non trouvé - pas de statistiques de temps
                        results[slug] = (0, None)
            except Exception as e:
                logger.error(f"Error batch fetching game times: {e}")
                # Ajouter des valeurs par défaut pour les jeux dans ce chunk en cas d'erreur
                for slug in chunk:
                    if slug not in results:
                        results[slug] = (0, None)
        
        return results

    async def get_friends(self) -> Dict[str, str]:
        query = """
        query {
            me {
                friends {
                    items {
                        player {
                            pd
                            psd
                            displayName
                        }
                    }
                }
            }
        }
        """
        data = await self._make_graphql_request(query)
        return {
            user_json['player']['pd']: user_json["player"]["displayName"]
            for user_json in data["me"]["friends"]["items"]
        }

    async def get_lastplayed_games(self, game_slugs: List[GameSlug]) -> Dict[GameSlug, Timestamp]:
        if not game_slugs:
            return {}
            
        query = """
        query($gameSlugs: [String!]!) {
            me {
                recentGames(gameSlugs: $gameSlugs) {
                    items {
                        gameSlug
                        lastSessionEndDate
                    }
                }
            }
        }
        """
        data = await self._make_graphql_request(query, {"gameSlugs": game_slugs})
        games = data["me"]["recentGames"]["items"]
        return {
            game["gameSlug"]: self._parse_timestamp(game["lastSessionEndDate"])
            for game in games
        }

    @staticmethod
    def _parse_timestamp(timestamp: str) -> Timestamp:
        return Timestamp(int((datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ") - datetime(1970, 1, 1)).total_seconds()))

    async def get_subscriptions(self) -> List[Subscription]:
        query = """
        query {
            me {
                subscriptions {
                    offerId
                    recurring
                    start
                    end
                    level
                    status
                    offer {
                        offerName
                        duration
                    }
                    platform
                    type
                    statusReasonCode
                    acquisitionMethod
                }
            }
        }
        """
        data = await self._make_graphql_request(query)
        subscriptions = data['me']['subscriptions']
        subs = {
            'standard': Subscription(subscription_name='EA Play', owned=False),
            'premium': Subscription(subscription_name='EA Play Pro', owned=False)
        }
        
        for sub in subscriptions:
            if sub['status'].startswith('ACTIVE'):
                tier = sub['level'].lower()
                if tier in subs:
                    subs[tier].owned = True
                    subs[tier].end_time = self._parse_timestamp(sub['end'])
                    
        return list(subs.values())

    async def get_games_in_subscription(self, tier: str) -> List[SubscriptionGame]:
        tier_mapping = {
            'standard': "ORIGIN_ACCESS_BASIC",
            'premium': "ORIGIN_ACCESS_PREMIER"
        }
        check_mapping = {
            'standard': ["ea-play"],
            'premium': ["ea-play-pro", "ea-play"]
        }
        
        query = """
        query($tier: LifecycleType!) {
            gameSearch(filter: {gameTypes: [BASE_GAME], productLifecycleFilter: {lifecycleTypes: [$tier]}}, paging: {limit: 9999}) {
                items {
                    slug
                }
            }
        }
        """
        data = await self._make_graphql_request(query, {"tier": tier_mapping[tier]})
        slugs = [game['slug'] for game in data['gameSearch']['items']]
        
        max_batch_size = 50
        all_games = []
        
        for i in range(0, len(slugs), max_batch_size):
            batch_slugs = slugs[i:i + max_batch_size]
            query = """
            query($slugs: [String!]!) {
                games(slugs: $slugs) {
                    items {
                        slug
                        products {
                            items {
                                id
                                name
                                originOfferId
                            }
                        }
                    }
                }
            }
            """
            
            batch_games = await self._make_graphql_request(query, {"slugs": batch_slugs})
            all_games.extend(batch_games['games']['items'])
            
        games = {'games': {'items': all_games}}
        subscription_games = []
        
        for game in games['games']['items']:
            if len(game['products']['items']) == 1:
                subscription_games.append(
                    SubscriptionGame(
                        game_title=game['products']['items'][0]['name'],
                        game_id=game['products']['items'][0]['originOfferId'] + '@subscription'
                    )
                )
            else:
                for product in game['products']['items']:
                    if any(check in product['id'] for check in check_mapping[tier]):
                        subscription_games.append(
                            SubscriptionGame(
                                game_title=product['name'],
                                game_id=product['originOfferId'] + '@subscription'
                            )
                        )
                        
        return subscription_games
        
    def clear_cache(self) -> None:
        """Clear the response cache"""
        self._response_cache = {}
        self._cache_timestamps = {}