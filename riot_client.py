import aiohttp

class RiotClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = None

    async def init_session(self):
        """aiohttp 클라이언트 세션 초기화"""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()

    async def close_session(self):
        """세션 종료"""
        if self.session and not self.session.closed:
            await self.session.close()

    async def get_account_by_riot_id(self, game_name: str, tag_line: str):
        """Riot ID(게임이름#태그)로 계정 정보(PUUID) 조회"""
        await self.init_session()
        url = f"https://asia.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
        headers = {"X-Riot-Token": self.api_key}
        
        async with self.session.get(url, headers=headers) as resp:
            if resp.status == 200:
                return await resp.json()
            return None

    async def get_summoner_by_puuid(self, puuid: str):
        """PUUID로 소환사 기본 정보 조회"""
        await self.init_session()
        url = f"https://kr.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{puuid}"
        headers = {"X-Riot-Token": self.api_key}
        
        async with self.session.get(url, headers=headers) as resp:
            if resp.status == 200:
                return await resp.json()
            return None

    async def get_league_entries(self, puuid: str):
        """PUUID로 랭크 티어/전적 정보 조회"""
        await self.init_session()
        url = f"https://kr.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
        headers = {"X-Riot-Token": self.api_key}
        
        async with self.session.get(url, headers=headers) as resp:
            if resp.status == 200:
                return await resp.json()
            return []

    async def get_recent_matches(self, puuid: str, count: int = 5):
        """최근 매치 ID 목록 조회"""
        await self.init_session()
        url = f"https://asia.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?start=0&count={count}"
        headers = {"X-Riot-Token": self.api_key}
        
        async with self.session.get(url, headers=headers) as resp:
            if resp.status == 200:
                return await resp.json()
            return []

    async def get_match_detail(self, match_id: str):
        """매치 상세 정보 조회"""
        await self.init_session()
        url = f"https://asia.api.riotgames.com/lol/match/v5/matches/{match_id}"
        headers = {"X-Riot-Token": self.api_key}
        
        async with self.session.get(url, headers=headers) as resp:
            if resp.status == 200:
                return await resp.json()
            return None

    async def get_active_game(self, puuid: str):
        """현재 진행 중인 게임(관전) 정보 조회"""
        await self.init_session()
        url = f"https://kr.api.riotgames.com/lol/spectator/v5/active-games/by-summoner/{puuid}"
        headers = {"X-Riot-Token": self.api_key}
        
        async with self.session.get(url, headers=headers) as resp:
            if resp.status == 404:
                return None  # 현재 게임 중이 아님
            if resp.status == 200:
                return await resp.json()
            return None
