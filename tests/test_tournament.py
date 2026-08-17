import unittest

from tournament_scouting import draft_card, opponent_report, resolve_opponent, team_report


class TournamentScoutingTest(unittest.TestCase):
    def test_known_opponent_resolves(self):
        opponent = resolve_opponent("아산드림윙즈")
        self.assertIsNotNone(opponent)
        self.assertEqual(opponent.representative, "zmdff#123")

    def test_opponent_report_contains_evidence_and_composition(self):
        report = opponent_report("아산드림윙즈")
        self.assertIn("럼블–신 짜오–갈리오–카이사–럭스", report)
        self.assertIn("근거", report)
        self.assertIn("예선 5인 경기 확인", report)

    def test_unknown_team_returns_helpful_list(self):
        report = opponent_report("없는팀")
        self.assertIn("팀을 찾지 못했습니다", report)
        self.assertIn("TEAM 91", report)

    def test_draft_card_respects_side_and_preserves_policy_boundary(self):
        blue = draft_card("TEAM 91", "블루")
        red = draft_card("TEAM 91", "레드")
        self.assertIn("블루 진영", blue)
        self.assertIn("레드 진영", red)
        self.assertIn("3밴", blue)
        self.assertIn("실시간 게임 정보는 수집하지 않음", blue)

    def test_team_report_is_pre_game_only(self):
        report = team_report()
        self.assertIn("T1 Viper", report)
        self.assertIn("경기 종료 후", report)
        self.assertIn("수집하거나 지시하지 않는다", report)


if __name__ == "__main__":
    unittest.main()
