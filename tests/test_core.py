import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from bot import build_composition_embed, build_performance_embed, build_review_embed
from gemini_analyzer import CoachingReport, GeminiAnalyzer
from riot_client import RiotClient


PUUID = "player-puuid"


def match_payload(match_id: str, *, champion: str, win: bool, kills: int, deaths: int, assists: int):
    return {
        "metadata": {"matchId": match_id},
        "info": {
            "gameDuration": 1800,
            "queueId": 420,
            "gameMode": "CLASSIC",
            "gameCreation": 1_700_000_000_000,
            "participants": [
                {
                    "puuid": PUUID,
                    "participantId": 1,
                    "championName": champion,
                    "teamPosition": "MIDDLE",
                    "win": win,
                    "kills": kills,
                    "deaths": deaths,
                    "assists": assists,
                    "totalMinionsKilled": 180,
                    "neutralMinionsKilled": 10,
                    "totalDamageDealtToChampions": 25000,
                    "totalDamageTaken": 18000,
                    "visionScore": 20,
                    "goldEarned": 12000,
                }
            ],
        },
    }


class RiotClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_recent_performance_aggregates_match_v5_data(self):
        client = RiotClient(api_key="test-key")
        payloads = {
            "KR_1": match_payload("KR_1", champion="Ahri", win=True, kills=8, deaths=2, assists=6),
            "KR_2": match_payload("KR_2", champion="Ahri", win=False, kills=2, deaths=4, assists=3),
        }

        async def recent_matches(*args, **kwargs):
            return ["KR_1", "KR_2"]

        async def match_detail(match_id):
            return payloads[match_id]

        client.get_recent_matches = recent_matches
        client.get_match_detail = match_detail

        performance = await client.get_recent_performance(PUUID, count=5, queue=420)

        self.assertEqual(performance["games"], 2)
        self.assertEqual(performance["wins"], 1)
        self.assertEqual(performance["win_rate"], 50.0)
        self.assertEqual(performance["main_champion"], "Ahri")
        self.assertEqual(performance["primary_role"], "MIDDLE")
        self.assertEqual(performance["average_cs_per_minute"], 6.3)

    async def test_match_review_extracts_player_events(self):
        client = RiotClient(api_key="test-key")
        detail = match_payload("KR_3", champion="Ahri", win=False, kills=2, deaths=3, assists=4)
        timeline = {
            "info": {
                "frames": [
                    {
                        "events": [
                            {"type": "CHAMPION_KILL", "timestamp": 420000, "victimId": 1},
                            {"type": "CHAMPION_KILL", "timestamp": 510000, "killerId": 1},
                            {
                                "type": "ELITE_MONSTER_KILL",
                                "timestamp": 1200000,
                                "killerId": 1,
                                "monsterType": "DRAGON",
                            },
                        ]
                    }
                ]
            }
        }

        review = client.build_match_review_data(detail, timeline, PUUID)

        self.assertTrue(review["timeline_available"])
        self.assertEqual(review["first_death_time"], "7:00")
        self.assertEqual(review["kill_count_in_timeline"], 1)
        self.assertIn("DRAGON", review["personal_objectives"][0]["detail"])


class PresentationTest(unittest.TestCase):
    def test_one_liner_is_separated_from_markdown(self):
        one_liner, markdown = GeminiAnalyzer._extract_one_liner(
            "한줄 피드백: 초반 데스를 줄이고 미드 웨이브 관리에 집중하세요.\n## 총평\n좋습니다."
        )
        self.assertEqual(one_liner, "초반 데스를 줄이고 미드 웨이브 관리에 집중하세요.")
        self.assertEqual(markdown, "## 총평\n좋습니다.")

    def test_invalid_model_prefix_is_replaced_with_safe_fallback(self):
        one_liner, markdown = GeminiAnalyzer._extract_one_liner(
            "-->52 chars. Perfect.\n## 총평\n형식이 섞인 응답"
        )
        self.assertIn("형식을 확인하지 못했습니다", one_liner)
        self.assertIn("상세 코칭을 표시하지 않았습니다", markdown)

    def test_composition_embed_uses_only_manual_champion_lists(self):
        embed = build_composition_embed(
            display_name="수동 조합",
            my_champion="아리",
            my_team=["아리", "리 신", "오른", "징크스", "룰루"],
            enemy_team=["제드", "바이", "나르", "카이사", "레오나"],
            advice="## 내 역할\n테스트 조언",
        )
        self.assertIn("챔피언 조합 코칭", embed.title)
        self.assertIn("아리", embed.fields[0].value)
        self.assertIn("실시간 상태", embed.footer.text)

    def test_embeds_contain_core_metrics(self):
        report = CoachingReport(one_liner="라인 주도권을 안정적으로 전환하세요.", markdown="## 총평\n테스트 리포트")
        performance = {
            "games": 5,
            "wins": 3,
            "losses": 2,
            "win_rate": 60.0,
            "average_kills": 5.0,
            "average_deaths": 3.0,
            "average_assists": 7.0,
            "average_kda_ratio": 4.0,
            "main_champion": "Ahri",
            "main_champion_games": 3,
            "primary_role": "MIDDLE",
            "average_cs_per_minute": 6.5,
        }
        embed = build_performance_embed(
            summoner_name="테스터#KR1",
            queue_name="솔로 랭크",
            performance=performance,
            rank_text="솔로: GOLD II 30LP",
            report=report,
            champion_icon_url=None,
        )
        self.assertIn("60.0%", embed.fields[0].value)
        self.assertIn("Ahri", embed.fields[1].value)

        review_embed = build_review_embed(
            summoner_name="테스터#KR1",
            review_data={
                "player_match": {
                    "champion": "Ahri",
                    "win": False,
                    "game_duration": "30:00",
                    "kills": 2,
                    "deaths": 3,
                    "assists": 4,
                    "damage_to_champions": 20000,
                },
                "timeline_available": True,
                "first_death_time": "7:00",
                "death_count_in_timeline": 3,
                "personal_objectives": [],
            },
            report=report,
            champion_icon_url=None,
        )
        self.assertIn("패배", review_embed.fields[0].value)


if __name__ == "__main__":
    unittest.main()


class OperationsGuardTest(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limiter_waits_for_second_window(self):
        client = RiotClient(api_key="test-key", rate_limit_per_second=1, rate_limit_per_two_minutes=10)
        await client._rate_limiter.acquire()

        with patch("riot_client.asyncio.sleep", new_callable=AsyncMock) as sleep:
            acquire_task = asyncio.create_task(client._rate_limiter.acquire())
            await asyncio.sleep(0)
            self.assertTrue(sleep.awaited)
            acquire_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await acquire_task

    async def test_timeline_selection_preserves_late_critical_event(self):
        events = [
            {"timestamp": timestamp * 60_000, "kind": "item", "time": f"{timestamp}:00", "detail": "ITEM"}
            for timestamp in range(1, 50)
        ]
        events.extend(
            [
                {"timestamp": 8 * 60_000, "kind": "death", "time": "8:00", "detail": "초반 사망"},
                {"timestamp": 44 * 60_000, "kind": "objective", "time": "44:00", "detail": "팀 BARON_NASHOR 처치"},
            ]
        )

        selected = RiotClient._select_notable_events(events, max_events=10)

        self.assertEqual(len(selected), 10)
        self.assertTrue(any(event["time"] == "44:00" for event in selected))
        self.assertTrue(any(event["kind"] == "death" for event in selected))


class _FakeResponse:
    text = "테스트 답변"


class _FakeChat:
    def send_message(self, _message):
        return _FakeResponse()


class _FakeChats:
    def create(self, **_kwargs):
        return _FakeChat()


class _FakeClient:
    chats = _FakeChats()


class GeminiSessionGuardTest(unittest.TestCase):
    def test_session_history_is_bounded(self):
        analyzer = GeminiAnalyzer(api_key="test-key")
        analyzer.client = _FakeClient()
        analyzer.start_coaching_session(1, "테스터#KR1", "전적 데이터", "초기 리포트")

        for index in range(GeminiAnalyzer.MAX_SESSION_TURNS + 5):
            analyzer.continue_coaching_session(1, f"질문 {index}")

        session = analyzer.sessions[1]
        self.assertLessEqual(len(session.history), GeminiAnalyzer.MAX_SESSION_HISTORY_MESSAGES)
        self.assertEqual(session.turn_count, GeminiAnalyzer.MAX_SESSION_TURNS + 5)


class SpectatorRetirementTest(unittest.TestCase):
    def test_riot_client_has_no_active_game_lookup(self):
        self.assertFalse(hasattr(RiotClient, "get_active_game_for_puuid"))
        self.assertFalse(hasattr(RiotClient, "get_active_game_by_summoner_id"))


class TimelineCoachingRulesTest(unittest.TestCase):
    def test_review_data_has_phase_summaries_and_evidence_patterns(self):
        client = RiotClient(api_key="test-key")
        detail = match_payload("KR_4", champion="Ahri", win=False, kills=2, deaths=3, assists=4)
        detail["info"]["participants"] = [
            {**detail["info"]["participants"][0], "teamId": 100},
            {"puuid": "enemy-puuid", "participantId": 2, "teamId": 200, "championName": "Zed"},
        ]
        timeline = {
            "info": {
                "frames": [
                    {
                        "events": [
                            {"type": "CHAMPION_KILL", "timestamp": 8 * 60_000, "victimId": 1},
                            {"type": "CHAMPION_KILL", "timestamp": 9 * 60_000, "victimId": 1},
                            {"type": "CHAMPION_KILL", "timestamp": 16 * 60_000, "victimId": 1},
                            {
                                "type": "ELITE_MONSTER_KILL",
                                "timestamp": 16 * 60_000 + 30_000,
                                "killerId": 2,
                                "killerTeamId": 200,
                                "monsterType": "DRAGON",
                            },
                            {
                                "type": "BUILDING_KILL",
                                "timestamp": 20 * 60_000,
                                "killerId": 1,
                                "teamId": 200,
                                "buildingType": "TOWER_BUILDING",
                            },
                            {
                                "type": "ELITE_MONSTER_KILL",
                                "timestamp": 31 * 60_000,
                                "killerId": 2,
                                "killerTeamId": 200,
                                "monsterType": "BARON_NASHOR",
                            },
                        ]
                    }
                ]
            }
        }

        review = client.build_match_review_data(detail, timeline, PUUID)

        self.assertEqual(review["phase_summaries"]["early"]["player_deaths"], 2)
        self.assertEqual(review["phase_summaries"]["mid"]["enemy_objectives"], 1)
        self.assertEqual(review["phase_summaries"]["late"]["enemy_objectives"], 1)
        self.assertTrue(any(event.get("objective_type") == "BARON_NASHOR" for event in review["notable_events"]))
        self.assertTrue(any(pattern["pattern"] == "early_first_death" for pattern in review["detected_patterns"]))
        self.assertTrue(any(pattern["pattern"] == "clustered_deaths" for pattern in review["detected_patterns"]))
        self.assertTrue(any(pattern["pattern"] == "objective_window_death" for pattern in review["detected_patterns"]))

    def test_event_priority_favors_late_baron_over_item_purchase(self):
        late_baron = {
            "timestamp": 30 * 60_000,
            "phase": "late",
            "kind": "objective",
            "team": "enemy",
            "objective_type": "BARON_NASHOR",
        }
        item = {"timestamp": 30 * 60_000, "phase": "late", "kind": "item"}

        self.assertGreater(RiotClient._event_priority(late_baron), RiotClient._event_priority(item))
