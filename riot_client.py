"""
Riot Games API 클라이언트 (수정판)

원본 대비 수정 사항:
1. [버그 수정] 200이 아닌 응답을 전부 None/[]로 뭉개지 않고, RiotAPIError(status, message)로
   구분해서 던집니다. bot.py에서 "소환사 없음(404)"과 "키 만료(401/403)", "레이트리밋(429)"을
   서로 다른 메시지로 안내할 수 있습니다.
2. [기능 추가] get_champion_name() — Data Dragon에서 챔피언 목록을 받아
   championId(숫자) -> 한글 챔피언 이름 매핑을 제공합니다. Spectator/Summoner API는
   챔피언 이름을 안 주고 숫자 ID만 주기 때문에 필요합니다.
3. [정책 변경 안내] get_active_game() — 2025-10(패치 25.20)부터 라이엇이 익명화 정책과
   맞물려 LoL용 Spectator-V5 API를 단계적으로 제한하고 있습니다. 403을 받으면 그대로
   위로 올려서 bot.py가 "라이엇 정책 변경으로 제한됨" 안내를 하도록 했습니다.
"""
import time

import aiohttp


class RiotAPIError(Exception):
    """Riot API가 200이 아닌 응답을 줬을 때 발생. status로 원인을 구분할 수 있습니다."""

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"[{status}] {message}")


class RiotClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session: aiohttp.ClientSession | None = None
        self._champion_map: dict[str, str] | None = None  # championId(str) -> 한글 이름
        self._champion_map_fetched_at = 0.0

    async def init_session(self):
        """aiohttp 클라이언트 세션 초기화"""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()

    async def close_session(self):
        """세션 종료"""
        if self.session and not self.session.closed:
            await self.session.close()

    async def _get(self, url: str):
        """공통 GET 요청. 실패 시 상태코드를 담아 RiotAPIError를 던집니다."""
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

    # ---- Account-V1 (지역 라우팅: asia) ----
    async def get_account_by_riot_id(self, game_name: str, tag_line: str):
        """Riot ID(게임이름#태그)로 계정 정보(PUUID) 조회"""
        url = f"https://asia.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
        return await self._get(url)

    # ---- Summoner-V4 (플랫폼 라우팅: kr) ----
    async def get_summoner_by_puuid(self, puuid: str):
        """PUUID로 소환사 기본 정보 조회"""
        url = f"https://kr.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{puuid}"
        return await self._get(url)

    # ---- League-V4 (플랫폼 라우팅: kr) ----
    async def get_league_entries(self, puuid: str):
        """PUUID로 랭크 티어 정보 조회. 언랭이면 빈 리스트."""
        url = f"https://kr.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
        try:
            return await self._get(url)
        except RiotAPIError as e:
            if e.status == 404:
                return []
            raise

    # ---- Match-V5 (지역 라우팅: asia) ----
    async def get_recent_matches(self, puuid: str, count: int = 5):
        """최근 매치 ID 목록 조회"""
        url = (
            f"https://asia.api.riotgames.com/lol/match/v5/matches/by-puuid/"
            f"{puuid}/ids?start=0&count={count}"
        )
        return await self._get(url)

    async def get_match_detail(self, match_id: str):
        """매치 상세 정보 조회 (KDA, 챔피언, 딜량, 승패 등 실제 데이터가 여기 들어있습니다)"""
        url = f"https://asia.api.riotgames.com/lol/match/v5/matches/{match_id}"
        return await self._get(url)

    # ---- Spectator-V5 (⚠️ 정책 변경으로 제한 가능성 있음 - 위 설명 참고) ----
    async def get_active_game(self, puuid: str):
        """현재 진행 중인 게임(관전) 정보 조회"""
        url = f"https://kr.api.riotgames.com/lol/spectator/v5/active-games/by-summoner/{puuid}"
        try:
            return await self._get(url)
        except RiotAPIError as e:
            if e.status == 404:
                return None  # 현재 게임 중이 아님 (정상 케이스)
            raise  # 401/403 등은 그대로 위로 올려서 호출부가 "정책 제한" 안내를 하게 함

    # ---- Data Dragon: championId(숫자) -> 한글 챔피언 이름 매핑 ----
    async def get_champion_name(self, champion_id) -> str:
        """
        Spectator API 등은 championId(숫자)만 주고 이름은 안 줍니다.
        Data Dragon에서 챔피언 목록을 받아와 매핑 테이블을 만들고 재사용합니다.
        (12시간 지나면 자동 갱신 - 새 패치로 챔피언이 추가될 수 있어서)
        """
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
