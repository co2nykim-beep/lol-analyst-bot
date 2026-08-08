import asyncio
import aiohttp
from cachetools import TTLCache

class RiotClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {
            "X-Riot-Token": self.api_key
        }
        self._cache = TTLCache(maxsize=100, ttl=300)
        self.champ_dict = {}

    async def init_champ_dict(self, session: aiohttp.ClientSession):
        """Data Dragon API를 활용해 챔피언 영문 ID -> 한글 이름 매핑 생성"""
        if self.champ_dict:
            return
        try:
            async with session.get("https://ddragon.leagueoflegends.com/api/versions.json") as resp:
                if resp.status == 200:
                    versions = await resp.json()
                    latest_version = versions[0]
                else:
                    latest_version = "14.1.1"

            champ_url = f"https://ddragon.leagueoflegends.com/cdn/{latest_version}/data/ko_KR/champion.json"
            async with session.get(champ_url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    champ_data = data.get("data", {})
                    for c_id, c_info in champ_data.items():
                        self.champ_dict[c_id] = c_info.get("name")
        except Exception as e:
            print(f"⚠️ 챔피언 한글 데이터 로드 실패: {e}")

    def _get_region_route(self, tag_line: str) -> str:
        tag = tag_line.upper().strip()
        if tag in ["KR", "JP", "KR1", "JP1"]:
            return "asia"
        elif tag in ["NA", "NA1", "BR", "LA1", "LA2"]:
            return "americas"
        elif tag in ["EUW", "EUNE", "TR", "RU"]:
            return "europe"
        return "asia"

    def _get_platform_route(self, tag_line: str) -> str:
        tag = tag_line.upper().strip()
        if tag.startswith("KR"):
            return "kr"
        elif tag.startswith("JP"):
            return "jp1"
        elif tag == "NA":
            return "na1"
        return "kr"

    async def _safe_get(self, session: aiohttp.ClientSession, url: str, retries: int = 3):
        """429 Rate Limit 지수 백오프 재시도 헬퍼"""
        for attempt in range(retries):
            async with session.get(url, headers=self.headers) as resp:
                if resp.status == 429:
                    await asyncio.sleep(1.2 * (attempt + 1))
                    continue
                return resp.status, await resp.json() if resp.status == 200 else None
        return 429, None

    async def fetch_account(self, session: aiohttp.ClientSession, game_name: str, tag_line: str):
        region = self._get_region_route(tag_line)
        cache_key = f"account:{game_name}#{tag_line}"
        if cache_key in self._cache:
            return self._cache[cache_key], 200

        url = f"https://{region}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
        status, data = await self._safe_get(session, url)
        if status == 200 and data:
            self._cache[cache_key] = data
            return data, 200
        return None, status

    async def fetch_league_entries(self, session: aiohttp.ClientSession, puuid: str, tag_line: str = "KR"):
        platform = self._get_platform_route(tag_line)
        cache_key = f"league:{puuid}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        league_url = f"https://{platform}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
        status, data = await self._safe_get(session, league_url)
        if status == 200 and data:
            self._cache[cache_key] = data
            return data
        return None

    async def _fetch_single_match(self, session: aiohttp.ClientSession, region: str, match_id: str, puuid: str):
        match_detail_url = f"https://{region}.api.riotgames.com/lol/match/v5/matches/{match_id}"
        status, detail_data = await self._safe_get(session, match_detail_url)
        if status != 200 or not detail_data:
            return None

        info = detail_data.get('info', {})
        participants = info.get('participants', [])

        for p in participants:
            if p.get('puuid') == puuid:
                eng_champ = p.get('championName', 'Unknown')
                kor_champ = self.champ_dict.get(eng_champ, eng_champ)

                return {
                    "champion": kor_champ,
                    "kills": p.get('kills'),
                    "deaths": p.get('deaths'),
                    "assists": p.get('assists'),
                    "win": p.get('win'),
                    "cs": p.get('totalMinionsKilled', 0) + p.get('neutralMinionsKilled', 0),
                    "damage": p.get('totalDamageDealtToChampions', 0),
                    "role": p.get('individualPosition', 'UNKNOWN')
                }
        return None

    async def get_user_recent_summary(self, session: aiohttp.ClientSession, puuid: str, region: str = "asia", count: int = 20):
        await self.init_champ_dict(session)
        
        cache_key = f"matches:{puuid}:{count}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        match_ids_url = f"https://{region}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?start=0&count={count}"
        status, match_ids = await self._safe_get(session, match_ids_url)
        if status != 200 or not match_ids:
            return []

        tasks = [self._fetch_single_match(session, region, m_id, puuid) for m_id in match_ids]
        results = await asyncio.gather(*tasks)

        summary = [r for r in results if r is not None]
        self._cache[cache_key] = summary
        return summary