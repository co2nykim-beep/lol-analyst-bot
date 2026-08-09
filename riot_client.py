import time

import aiohttp


class RiotAPIError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"[{status}] {message}")


class RiotClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session: aiohttp.ClientSession | None = None
        self._champion_map: dict[str, str] | None = None
        self._champion_map_fetched_at = 0.0

    async def init_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()

    async def close_session(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def _get(self, url: str):
        await self.init_session()
        headers = {"X-Riot-Token": self.api_key}
        async with self.session.get(url, headers=headers) as resp:
            if resp.status == 200:
                return await resp.json()
            if resp.status == 404:
                raise RiotAPIError(404, "데이터를 찾을 수 없습니다 (닉네임#태그를 다시 확인해주세요)")
            if resp.status in (401, 403):
                raise RiotAPIError(
                    resp.status,
                    "Riot API 키가 유효하지 않거나 만료되었습니다 (개발 키는 24시간마다 재발급 필요합니다)",
                )
            if resp.status == 429:
                retry_after = resp.headers.get("Retry-After", "알 수 없음")
                raise RiotAPIError(429, f"Riot API 레이트리밋에 걸렸습니다 ({retry_after}초 후 재시도)")
            text = await resp.text()
            raise RiotAPIError(resp.status, f"Riot API 오류: {text[:200]}")

    async def get_account_by_riot_id(self, game_name: str, tag_line: str):
        url = f"https://asia.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
        return await self._get(url)

    async def get_summoner_by_puuid(self, puuid: str):
        url = f"https://kr.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{puuid}"
        return await self._get(url)

    async def get_league_entries(self, puuid: str):
        url = f"https://kr.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
        try:
            return await self._get(url)
        except RiotAPIError as e:
            if e.status == 404:
                return []
            raise

    async def get_recent_matches(self, puuid: str, count: int = 10, queue: int | None = None):
        """
        소환사의 최근 매치 ID 목록을 가져옵니다.
        :param puuid: 소환사 고유 PUUID
        :param count: 조회할 매치 수 (기본값 10판)
        :param queue: Riot Queue ID (예: 420 = 솔로랭크, 440 = 자유랭크)
        """
        url = (
            f"https://asia.api.riotgames.com/lol/match/v5/matches/by-puuid/"
            f"{puuid}/ids?start=0&count={count}"
        )
        if queue is not None:
            url += f"&queue={queue}"
            
        return await self._get(url)

    async def get_match_detail(self, match_id: str):
        url = f"https://asia.api.riotgames.com/lol/match/v5/matches/{match_id}"
        return await self._get(url)

    async def get_active_game(self, puuid: str):
        url = f"https://kr.api.riotgames.com/lol/spectator/v5/active-games/by-summoner/{puuid}"
        try:
            return await self._get(url)
        except RiotAPIError as e:
            if e.status == 404:
                return None
            raise

    async def get_champion_name(self, champion_id) -> str:
        if self._champion_map is None or (time.time() - self._champion_map_fetched_at) > 43200:
            await self.init_session()
            async with self.session.get("https://ddragon.leagueoflegends.com/api/versions.json") as resp:
                versions = await resp.json()
            latest = versions[0]
            async with self.session.get(
                f"https://ddragon.leagueoflegends.com/cdn/{latest}/data/ko_KR/champion.json"
            ) as resp:
                data = await resp.json()
            self._champion_map = {v["key"]: v["name"] for v in data["data"].values()}
            self._champion_map_fetched_at = time.time()

        return self._champion_map.get(str(champion_id), f"챔피언#{champion_id}")
