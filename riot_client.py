"""Riot Games API 비동기 클라이언트.

이 모듈은 Riot ID 조회, Match-V5 전적/타임라인 수집, 그리고 디스코드·AI가
바로 사용할 수 있는 플레이어 중심 요약 데이터 생성을 담당한다.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter, deque
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import aiohttp
from cachetools import TTLCache


class RiotAPIError(Exception):
    """Riot API 응답을 사용자에게 안전하게 전달하기 위한 예외."""

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"[{status}] {message}")


class AsyncRequestLimiter:
    """Riot API의 짧은·긴 요청 창을 함께 지키는 프로세스 단위 제한기.

    기본값은 개인 키의 20회/초·100회/2분보다 여유를 둔 18회/초·90회/2분이다.
    요청이 몰릴 때 실패시키지 않고 호출 코루틴을 대기시켜 429를 예방한다.
    """

    def __init__(self, per_second: int = 18, per_two_minutes: int = 90) -> None:
        self.per_second = max(1, per_second)
        self.per_two_minutes = max(1, per_two_minutes)
        self._timestamps: deque[float] = deque()
        self._blocked_until = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """두 요청 창에 여유가 생길 때까지 기다린 뒤 하나의 슬롯을 예약한다."""
        while True:
            async with self._lock:
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] >= 120:
                    self._timestamps.popleft()

                recent_second = sum(1 for timestamp in self._timestamps if now - timestamp < 1)
                cooldown = max(0.0, self._blocked_until - now)
                if cooldown <= 0 and recent_second < self.per_second and len(self._timestamps) < self.per_two_minutes:
                    self._timestamps.append(now)
                    return

                waits = [cooldown]
                if recent_second >= self.per_second:
                    second_window = [timestamp for timestamp in self._timestamps if now - timestamp < 1]
                    waits.append(max(0.01, 1 - (now - second_window[0])))
                if len(self._timestamps) >= self.per_two_minutes:
                    waits.append(max(0.01, 120 - (now - self._timestamps[0])))
                delay = max(waits)
            await asyncio.sleep(delay)

    async def pause(self, seconds: float) -> None:
        """Riot의 Retry-After 기간 동안 새 호출이 시작되지 않게 한다."""
        async with self._lock:
            self._blocked_until = max(self._blocked_until, time.monotonic() + max(0.0, seconds))


class RiotClient:
    """한국 서버를 기본값으로 사용하는 Riot API 비동기 클라이언트."""

    DATA_DRAGON_BASE_URL = "https://ddragon.leagueoflegends.com"

    def __init__(
        self,
        api_key: str | None,
        platform_route: str = "kr",
        regional_route: str = "asia",
        request_timeout_seconds: int = 15,
        rate_limit_per_second: int = 18,
        rate_limit_per_two_minutes: int = 90,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.platform_route = platform_route.lower()
        self.regional_route = regional_route.lower()
        self.timeout = aiohttp.ClientTimeout(total=request_timeout_seconds)
        self.session: aiohttp.ClientSession | None = None
        self._data_dragon_version: str | None = None
        self._champion_id_to_name: dict[str, str] | None = None
        self._rate_limiter = AsyncRequestLimiter(
            per_second=rate_limit_per_second,
            per_two_minutes=rate_limit_per_two_minutes,
        )
        self._response_cache: TTLCache[str, tuple[float, Any]] = TTLCache(maxsize=2_000, ttl=86_400)

    async def init_session(self) -> None:
        """필요할 때만 공유 HTTP 세션을 생성한다."""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=self.timeout)

    async def close_session(self) -> None:
        """프로세스 종료 시 열려 있는 연결을 정리한다."""
        if self.session and not self.session.closed:
            await self.session.close()

    def _require_api_key(self) -> None:
        if not self.api_key:
            raise RiotAPIError(500, "RIOT_API_KEY 환경 변수가 설정되지 않았습니다.")

    async def _get(
        self,
        url: str,
        *,
        allow_not_found: bool = False,
        cache_ttl_seconds: float = 0.0,
    ) -> Any | None:
        """Riot API GET 호출, 프로세스 전역 레이트 리밋, 짧은 응답 캐시를 처리한다."""
        self._require_api_key()
        now = time.monotonic()
        if cache_ttl_seconds > 0:
            cached = self._response_cache.get(url)
            if cached and now - cached[0] < cache_ttl_seconds:
                return cached[1]

        await self.init_session()
        assert self.session is not None
        headers = {"X-Riot-Token": self.api_key}

        for attempt in range(2):
            await self._rate_limiter.acquire()
            try:
                async with self.session.get(url, headers=headers) as response:
                    if response.status == 200:
                        payload = await response.json()
                        if cache_ttl_seconds > 0:
                            self._response_cache[url] = (time.monotonic(), payload)
                        return payload
                    if response.status == 404 and allow_not_found:
                        return None
                    if response.status == 404:
                        raise RiotAPIError(404, "데이터를 찾을 수 없습니다. Riot ID와 태그를 확인해 주세요.")
                    if response.status in (401, 403):
                        raise RiotAPIError(
                            response.status,
                            "Riot API 키가 유효하지 않거나 만료되었습니다. 개발 키는 주기적으로 갱신해야 합니다.",
                        )
                    if response.status == 429:
                        retry_after_raw = response.headers.get("Retry-After", "1")
                        try:
                            retry_after = max(1.0, float(retry_after_raw))
                        except ValueError:
                            retry_after = 1.0
                        await self._rate_limiter.pause(retry_after)
                        if attempt == 0:
                            await asyncio.sleep(retry_after)
                            continue
                        raise RiotAPIError(
                            429,
                            f"Riot API 요청 한도에 도달했습니다. {retry_after:g}초 후 다시 시도해 주세요.",
                        )

                    body = (await response.text())[:200]
                    raise RiotAPIError(response.status, f"Riot API 요청이 실패했습니다: {body}")
            except aiohttp.ClientError as error:
                raise RiotAPIError(503, f"Riot API 연결에 실패했습니다: {error}") from error
            except asyncio.TimeoutError as error:
                raise RiotAPIError(504, "Riot API 응답 시간이 초과되었습니다. 잠시 후 다시 시도해 주세요.") from error

        raise RiotAPIError(429, "Riot API 요청 한도를 초과했습니다. 잠시 후 다시 시도해 주세요.")

    async def _get_public_json(self, url: str) -> dict[str, Any] | list[Any]:
        """API 키가 필요 없는 Data Dragon 데이터를 호출한다."""
        await self.init_session()
        assert self.session is not None
        try:
            async with self.session.get(url) as response:
                if response.status != 200:
                    raise RiotAPIError(response.status, "챔피언 이미지 정보를 불러오지 못했습니다.")
                return await response.json()
        except aiohttp.ClientError as error:
            raise RiotAPIError(503, f"Data Dragon 연결에 실패했습니다: {error}") from error

    def _platform_url(self, path: str) -> str:
        return f"https://{self.platform_route}.api.riotgames.com{path}"

    def _regional_url(self, path: str) -> str:
        return f"https://{self.regional_route}.api.riotgames.com{path}"

    async def get_account_by_riot_id(self, game_name: str, tag_line: str) -> dict[str, Any]:
        """Riot ID(`게임이름#태그`)로 계정과 PUUID를 조회한다."""
        encoded_game_name = quote(game_name.strip(), safe="")
        encoded_tag_line = quote(tag_line.strip(), safe="")
        url = self._regional_url(
            f"/riot/account/v1/accounts/by-riot-id/{encoded_game_name}/{encoded_tag_line}"
        )
        result = await self._get(url, cache_ttl_seconds=300)
        assert isinstance(result, dict)
        return result

    async def get_summoner_by_puuid(self, puuid: str) -> dict[str, Any]:
        """PUUID로 Summoner-V4 정보를 조회한다.

        Spectator-V5는 PUUID가 아닌 암호화된 summonerId를 요구하므로 이 조회가 필요하다.
        """
        url = self._platform_url(f"/lol/summoner/v4/summoners/by-puuid/{quote(puuid, safe='')}")
        result = await self._get(url, cache_ttl_seconds=900)
        assert isinstance(result, dict)
        return result

    async def get_league_entries(self, puuid: str) -> list[dict[str, Any]]:
        """솔로/자유 랭크 정보를 조회한다. 랭크 기록이 없으면 빈 목록을 반환한다."""
        url = self._platform_url(f"/lol/league/v4/entries/by-puuid/{quote(puuid, safe='')}")
        result = await self._get(url, allow_not_found=True, cache_ttl_seconds=120)
        return result if isinstance(result, list) else []

    async def get_recent_matches(
        self,
        puuid: str,
        count: int = 5,
        queue: int | None = None,
        start: int = 0,
    ) -> list[str]:
        """Match-V5에서 최근 매치 ID 목록을 가져온다.

        `queue`는 Riot queue ID(예: 420 솔로 랭크, 440 자유 랭크)이며, 서버 측 필터를
        적용하므로 이후 불필요한 상세 요청을 줄인다.
        """
        safe_count = max(1, min(int(count), 100))
        safe_start = max(0, int(start))
        query = f"start={safe_start}&count={safe_count}"
        if queue is not None:
            query += f"&queue={int(queue)}"

        url = self._regional_url(
            f"/lol/match/v5/matches/by-puuid/{quote(puuid, safe='')}/ids?{query}"
        )
        result = await self._get(url, cache_ttl_seconds=30)
        return [str(match_id) for match_id in result] if isinstance(result, list) else []

    async def get_match_detail(self, match_id: str) -> dict[str, Any]:
        """단일 매치의 Match-V5 상세 데이터를 가져온다."""
        url = self._regional_url(f"/lol/match/v5/matches/{quote(match_id, safe='')}")
        result = await self._get(url, cache_ttl_seconds=86_400)
        assert isinstance(result, dict)
        return result

    async def get_match_timeline(self, match_id: str) -> dict[str, Any] | None:
        """매치의 이벤트 타임라인을 가져온다.

        일부 게임 모드나 오래된 매치는 타임라인이 없을 수 있으므로 이 경우 `None`을 반환한다.
        """
        url = self._regional_url(f"/lol/match/v5/matches/{quote(match_id, safe='')}/timeline")
        result = await self._get(url, allow_not_found=True, cache_ttl_seconds=86_400)
        return result if isinstance(result, dict) else None

    async def get_active_game_by_summoner_id(self, summoner_id: str) -> dict[str, Any] | None:
        """Spectator-V5에서 진행 중 게임의 로스터를 조회한다."""
        url = self._platform_url(
            f"/lol/spectator/v5/active-games/by-summoner/{quote(summoner_id, safe='')}"
        )
        result = await self._get(url, allow_not_found=True, cache_ttl_seconds=15)
        return result if isinstance(result, dict) else None

    async def get_active_game_for_puuid(self, puuid: str) -> dict[str, Any] | None:
        """PUUID를 입력받아 진행 중 게임 로스터를 조회하는 편의 메서드."""
        summoner = await self.get_summoner_by_puuid(puuid)
        summoner_id = summoner.get("id")
        if not summoner_id:
            raise RiotAPIError(404, "진행 중 게임 조회에 필요한 소환사 정보를 찾지 못했습니다.")
        return await self.get_active_game_by_summoner_id(str(summoner_id))

    async def get_data_dragon_version(self) -> str:
        """현재 챔피언 아이콘 URL에 사용할 Data Dragon 버전을 캐시한다."""
        if self._data_dragon_version:
            return self._data_dragon_version

        versions = await self._get_public_json(f"{self.DATA_DRAGON_BASE_URL}/api/versions.json")
        if not isinstance(versions, list) or not versions:
            raise RiotAPIError(503, "Data Dragon 버전 정보를 찾지 못했습니다.")
        self._data_dragon_version = str(versions[0])
        return self._data_dragon_version

    async def get_champion_icon_url(self, champion_name: str) -> str:
        """챔피언명에 대응하는 Data Dragon 정사각형 아이콘 URL을 반환한다."""
        version = await self.get_data_dragon_version()
        return f"{self.DATA_DRAGON_BASE_URL}/cdn/{version}/img/champion/{quote(champion_name, safe='')}.png"

    async def get_champion_name(self, champion_id: int | str) -> str:
        """Spectator-V5의 숫자 championId를 한국어 챔피언명으로 변환한다."""
        if self._champion_id_to_name is None:
            version = await self.get_data_dragon_version()
            data = await self._get_public_json(
                f"{self.DATA_DRAGON_BASE_URL}/cdn/{version}/data/ko_KR/champion.json"
            )
            champions = data.get("data", {}) if isinstance(data, dict) else {}
            self._champion_id_to_name = {
                str(champion.get("key")): str(champion.get("name"))
                for champion in champions.values()
                if isinstance(champion, dict) and champion.get("key") and champion.get("name")
            }
        return self._champion_id_to_name.get(str(champion_id), f"챔피언 #{champion_id}")

    @staticmethod
    def _format_timestamp(milliseconds: int | float | None) -> str:
        total_seconds = max(0, int((milliseconds or 0) / 1000))
        return f"{total_seconds // 60}:{total_seconds % 60:02d}"

    @staticmethod
    def _find_participant(match_detail: dict[str, Any], puuid: str) -> dict[str, Any]:
        participants = match_detail.get("info", {}).get("participants", [])
        participant = next((item for item in participants if item.get("puuid") == puuid), None)
        if not isinstance(participant, dict):
            raise RiotAPIError(404, "해당 소환사를 이 매치에서 찾지 못했습니다.")
        return participant

    def parse_match_for_player(self, match_detail: dict[str, Any], puuid: str) -> dict[str, Any]:
        """원본 Match-V5 응답에서 특정 플레이어 기준의 코칭용 지표를 추출한다."""
        info = match_detail.get("info", {})
        participant = self._find_participant(match_detail, puuid)

        kills = int(participant.get("kills", 0))
        deaths = int(participant.get("deaths", 0))
        assists = int(participant.get("assists", 0))
        cs = int(participant.get("totalMinionsKilled", 0)) + int(
            participant.get("neutralMinionsKilled", 0)
        )
        duration_seconds = max(1, int(info.get("gameDuration", 0)))
        started_at = info.get("gameCreation")
        started_at_iso = None
        if isinstance(started_at, (int, float)) and started_at > 0:
            started_at_iso = datetime.fromtimestamp(started_at / 1000, tz=UTC).isoformat()

        return {
            "match_id": str(match_detail.get("metadata", {}).get("matchId", "")),
            "champion": str(participant.get("championName", "알 수 없음")),
            "role": str(
                participant.get("teamPosition")
                or participant.get("individualPosition")
                or participant.get("lane")
                or "UNKNOWN"
            ),
            "win": bool(participant.get("win", False)),
            "kills": kills,
            "deaths": deaths,
            "assists": assists,
            "kda_ratio": round((kills + assists) / max(1, deaths), 2),
            "damage_to_champions": int(participant.get("totalDamageDealtToChampions", 0)),
            "damage_taken": int(participant.get("totalDamageTaken", 0)),
            "vision_score": int(participant.get("visionScore", 0)),
            "cs": cs,
            "cs_per_minute": round(cs / (duration_seconds / 60), 1),
            "gold_earned": int(participant.get("goldEarned", 0)),
            "game_duration_seconds": duration_seconds,
            "game_duration": self._format_timestamp(duration_seconds * 1000),
            "queue_id": int(info.get("queueId", 0)),
            "game_mode": str(info.get("gameMode", "")),
            "game_started_at": started_at_iso,
        }

    async def get_recent_match_summaries(
        self,
        puuid: str,
        count: int = 5,
        queue: int | None = None,
    ) -> list[dict[str, Any]]:
        """최근 매치 상세를 제한된 동시성으로 가져와 플레이어 기준으로 파싱한다."""
        match_ids = await self.get_recent_matches(puuid=puuid, count=count, queue=queue)
        if not match_ids:
            return []

        semaphore = asyncio.Semaphore(3)

        async def fetch_one(match_id: str) -> dict[str, Any] | None:
            async with semaphore:
                detail = await self.get_match_detail(match_id)
                return self.parse_match_for_player(detail, puuid)

        results = await asyncio.gather(*(fetch_one(match_id) for match_id in match_ids), return_exceptions=True)
        summaries: list[dict[str, Any]] = []
        for result in results:
            if isinstance(result, RiotAPIError):
                raise result
            if isinstance(result, Exception):
                raise RiotAPIError(500, f"매치 상세 데이터를 처리하지 못했습니다: {result}") from result
            if result:
                summaries.append(result)
        return summaries

    async def get_recent_performance(
        self,
        puuid: str,
        count: int = 5,
        queue: int | None = None,
    ) -> dict[str, Any]:
        """최근 전적을 요약해 Gemini 프롬프트와 Embed에 바로 사용할 수 있게 만든다."""
        matches = await self.get_recent_match_summaries(puuid=puuid, count=count, queue=queue)
        if not matches:
            return {"games": 0, "matches": []}

        games = len(matches)
        wins = sum(1 for match in matches if match["win"])
        champion_counts = Counter(match["champion"] for match in matches)
        role_counts = Counter(match["role"] for match in matches if match["role"] != "UNKNOWN")
        main_champion, main_champion_games = champion_counts.most_common(1)[0]
        total_kills = sum(match["kills"] for match in matches)
        total_deaths = sum(match["deaths"] for match in matches)
        total_assists = sum(match["assists"] for match in matches)

        return {
            "games": games,
            "wins": wins,
            "losses": games - wins,
            "win_rate": round((wins / games) * 100, 1),
            "average_kills": round(total_kills / games, 1),
            "average_deaths": round(total_deaths / games, 1),
            "average_assists": round(total_assists / games, 1),
            "average_kda_ratio": round((total_kills + total_assists) / max(1, total_deaths), 2),
            "average_damage_to_champions": round(
                sum(match["damage_to_champions"] for match in matches) / games
            ),
            "average_cs_per_minute": round(sum(match["cs_per_minute"] for match in matches) / games, 1),
            "main_champion": main_champion,
            "main_champion_games": main_champion_games,
            "primary_role": role_counts.most_common(1)[0][0] if role_counts else "UNKNOWN",
            "champion_pool": [
                {"champion": champion, "games": played}
                for champion, played in champion_counts.most_common(3)
            ],
            "matches": matches,
        }

    @staticmethod
    def _phase_from_timestamp(timestamp: int) -> str:
        """경기 시간을 초반·중반·후반의 고정 코칭 구간으로 분류한다."""
        if timestamp < 10 * 60_000:
            return "early"
        if timestamp < 25 * 60_000:
            return "mid"
        return "late"

    @staticmethod
    def _event_priority(event: dict[str, Any]) -> int:
        """사건의 복기 우선순위를 정한다. 값은 모델 입력 선별용이며 실력 점수가 아니다."""
        kind = str(event.get("kind", ""))
        priority = {"death": 100, "objective": 85, "kill": 60, "assist": 45, "item": 5}.get(kind, 0)
        phase = str(event.get("phase", ""))
        objective_type = str(event.get("objective_type", ""))

        if kind == "objective":
            priority += {"ELDER_DRAGON": 60, "BARON_NASHOR": 50, "DRAGON": 30, "RIFTHERALD": 25}.get(
                objective_type, 15
            )
            if event.get("team") == "enemy":
                priority += 10
        if kind == "death" and phase == "late":
            priority += 25
        if kind in {"kill", "assist"} and phase == "late":
            priority += 10
        return priority

    @classmethod
    def _select_notable_events(cls, events: list[dict[str, Any]], max_events: int = 35) -> list[dict[str, Any]]:
        """중요도와 고정 시간대를 함께 보장해 복기 장면을 선별한다."""
        if not events:
            return []

        selected_indices: set[int] = set()
        # 초반·중반·후반 각각의 대표 사건을 먼저 확보해 한 구간으로 분석이 치우치지 않게 한다.
        for phase in ("early", "mid", "late"):
            candidates = [
                (index, event)
                for index, event in enumerate(events)
                if event.get("phase") == phase
            ]
            if candidates:
                selected_indices.add(
                    max(
                        candidates,
                        key=lambda item: (cls._event_priority(item[1]), int(item[1]["timestamp"])),
                    )[0]
                )

        remaining = sorted(
            enumerate(events),
            key=lambda item: (-cls._event_priority(item[1]), -int(item[1]["timestamp"])),
        )
        for index, _event in remaining:
            if len(selected_indices) >= max_events:
                break
            selected_indices.add(index)

        return [
            event
            for _index, event in sorted(
                ((index, events[index]) for index in selected_indices),
                key=lambda item: int(item[1]["timestamp"]),
            )
        ]

    @classmethod
    def _build_phase_summaries(cls, events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """모델이 시간대별 흐름을 비교할 수 있는 최소한의 정량 근거를 만든다."""
        phase_labels = {"early": "초반 (0~10분)", "mid": "중반 (10~25분)", "late": "후반 (25분 이후)"}
        summaries: dict[str, dict[str, Any]] = {}
        for phase, label in phase_labels.items():
            phase_events = [event for event in events if event.get("phase") == phase]
            deaths = [event for event in phase_events if event["kind"] == "death"]
            objectives = [event for event in phase_events if event["kind"] == "objective"]
            summaries[phase] = {
                "label": label,
                "player_deaths": len(deaths),
                "player_kills": sum(1 for event in phase_events if event["kind"] == "kill"),
                "player_assists": sum(1 for event in phase_events if event["kind"] == "assist"),
                "ally_objectives": sum(1 for event in objectives if event.get("team") == "ally"),
                "enemy_objectives": sum(1 for event in objectives if event.get("team") == "enemy"),
                "key_events": cls._select_notable_events(phase_events, max_events=3),
            }
        return summaries

    @staticmethod
    def _detect_review_patterns(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """원본 이벤트로 검증 가능한 패턴만 찾아 모델의 과도한 추론을 방지한다."""
        patterns: list[dict[str, Any]] = []
        deaths = [event for event in events if event["kind"] == "death"]
        objectives = [event for event in events if event["kind"] == "objective"]

        if deaths and deaths[0].get("phase") == "early":
            patterns.append(
                {
                    "pattern": "early_first_death",
                    "evidence": [deaths[0]],
                    "guidance": "초반 첫 사망 시점의 웨이브·시야·교전 진입 판단을 복기",
                }
            )

        for previous, current in zip(deaths, deaths[1:]):
            if int(current["timestamp"]) - int(previous["timestamp"]) <= 3 * 60_000:
                patterns.append(
                    {
                        "pattern": "clustered_deaths",
                        "evidence": [previous, current],
                        "guidance": "짧은 간격의 연속 사망 구간에서 복귀 후 재진입 판단을 복기",
                    }
                )
                break

        objective_window = 90_000
        for death in deaths:
            nearby_objective = next(
                (
                    objective
                    for objective in objectives
                    if abs(int(objective["timestamp"]) - int(death["timestamp"])) <= objective_window
                ),
                None,
            )
            if nearby_objective:
                patterns.append(
                    {
                        "pattern": "objective_window_death",
                        "evidence": [death, nearby_objective],
                        "guidance": "오브젝트 전후 사망이므로 합류·시야·교전 진입 타이밍을 복기",
                    }
                )
                break

        return patterns[:3]

    def build_match_review_data(
        self,
        match_detail: dict[str, Any],
        timeline: dict[str, Any] | None,
        puuid: str,
    ) -> dict[str, Any]:
        """Match-V5 Timeline을 시간대·우선순위·근거 중심의 복기 데이터로 변환한다."""
        player = self.parse_match_for_player(match_detail, puuid)
        participant = self._find_participant(match_detail, puuid)
        participant_id = int(participant.get("participantId", 0))
        player_team_id = int(participant.get("teamId", 0))
        participant_teams = {
            int(item.get("participantId", 0)): int(item.get("teamId", 0))
            for item in match_detail.get("info", {}).get("participants", [])
            if item.get("participantId")
        }
        events: list[dict[str, Any]] = []

        def team_label(team_id: int) -> str:
            if team_id == player_team_id:
                return "ally"
            return "enemy" if team_id else "unknown"

        def append_event(kind: str, timestamp: int, detail: str, **extra: Any) -> None:
            events.append(
                {
                    "timestamp": timestamp,
                    "time": self._format_timestamp(timestamp),
                    "phase": self._phase_from_timestamp(timestamp),
                    "kind": kind,
                    "detail": detail,
                    **extra,
                }
            )

        if timeline:
            for frame in timeline.get("info", {}).get("frames", []):
                for event in frame.get("events", []):
                    event_type = event.get("type")
                    timestamp = int(event.get("timestamp", 0))
                    if event_type == "CHAMPION_KILL":
                        if event.get("victimId") == participant_id:
                            append_event("death", timestamp, "챔피언 처치에 의해 사망")
                        elif event.get("killerId") == participant_id:
                            append_event("kill", timestamp, "챔피언 처치")
                        elif participant_id in event.get("assistingParticipantIds", []):
                            append_event("assist", timestamp, "챔피언 처치 어시스트")
                    elif event_type == "ITEM_PURCHASED" and event.get("participantId") == participant_id:
                        append_event("item", timestamp, "아이템 구매", item_id=event.get("itemId"))
                    elif event_type == "ELITE_MONSTER_KILL":
                        killer_id = int(event.get("killerId", 0))
                        killer_team_id = int(event.get("killerTeamId", 0)) or participant_teams.get(killer_id, 0)
                        monster_type = str(event.get("monsterType", "OBJECTIVE"))
                        if event.get("monsterSubType") == "ELDER_DRAGON":
                            monster_type = "ELDER_DRAGON"
                        append_event(
                            "objective",
                            timestamp,
                            f"{team_label(killer_team_id)} 팀 {monster_type} 처치",
                            team=team_label(killer_team_id),
                            objective_type=monster_type,
                            personal_contribution=killer_id == participant_id,
                        )
                    elif event_type == "BUILDING_KILL":
                        building_type = str(event.get("buildingType", "BUILDING"))
                        destroyed_team_id = int(event.get("teamId", 0))
                        killer_id = int(event.get("killerId", 0))
                        killer_team_id = participant_teams.get(killer_id, 0)
                        if not killer_team_id and destroyed_team_id in {100, 200}:
                            killer_team_id = 300 - destroyed_team_id
                        append_event(
                            "objective",
                            timestamp,
                            f"{team_label(killer_team_id)} 팀 {building_type} 파괴",
                            team=team_label(killer_team_id),
                            objective_type=building_type,
                            personal_contribution=killer_id == participant_id,
                        )

        deaths = sorted((event for event in events if event["kind"] == "death"), key=lambda event: int(event["timestamp"]))
        kills = [event for event in events if event["kind"] == "kill"]
        objectives = [event for event in events if event["kind"] == "objective"]
        personal_objectives = [event for event in objectives if event.get("personal_contribution")]
        notable_events = self._select_notable_events(events)
        phase_summaries = self._build_phase_summaries(events)
        detected_patterns = self._detect_review_patterns(events)

        return {
            "player_match": player,
            "timeline_available": timeline is not None,
            "first_death_time": deaths[0]["time"] if deaths else None,
            "death_count_in_timeline": len(deaths),
            "kill_count_in_timeline": len(kills),
            "team_objectives": objectives,
            "personal_objectives": personal_objectives,
            "phase_summaries": phase_summaries,
            "detected_patterns": detected_patterns,
            "timeline_event_count": len(events),
            "notable_event_count": len(notable_events),
            "notable_events": notable_events,
            "event_selection_policy": "초반·중반·후반 대표 장면 보장, 사망·주요 오브젝트·타워 우선",
        }

    async def get_match_review(self, match_id: str, puuid: str) -> dict[str, Any]:
        """단일 경기의 상세·타임라인을 결합해 복기용 데이터를 생성한다."""
        detail, timeline = await asyncio.gather(
            self.get_match_detail(match_id),
            self.get_match_timeline(match_id),
        )
        return self.build_match_review_data(detail, timeline, puuid)
""
