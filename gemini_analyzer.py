import re
from google import genai

def clean_latex(text: str) -> str:
    """LaTeX 표현 및 특수 태그 제거/정리"""
    if not text:
        return ""
    text = re.sub(r'\\(?:Large|large|huge|Huge|small|tiny|bf|it)\b', '', text)
    text = re.sub(r'\$\$?', '', text)
    return text.strip()

class GeminiAnalyzer:
    def __init__(self, api_key: str, model_name: str = "gemini-3.6-flash"):
        self.api_key = api_key
        self.model_name = model_name or "gemini-3.6-flash"
        self.client = genai.Client(api_key=api_key)

    def _generate(self, prompt: str) -> str:
        """Gemini API 호출 및 응답 처리"""
        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt
            )
            return clean_latex(response.text.strip())
        except Exception as e:
            return f"⚠️ AI 생성 실패: {e}"

    def analyze_match_history(self, summoner_name: str, match_summary_text: str) -> str:
        """최근 전적 및 피드백 분석"""
        prompt = f"""
리그 오브 레전드 AI 코치로서 소환사 '{summoner_name}'의 최근 전적 데이터를 분석해 주세요.

[전적 요약 데이터]
{match_summary_text}

다음 사항을 포함하여 디스코드에 보기 좋은 마크다운 형식으로 작성하세요:
1. **플레이 스타일 총평**: 주 라인, 챔피언 폭, KDA/승률 종합 평가
2. **강점 및 보완점**: 잘하고 있는 점과 개선이 필요한 부분
3. **AI 추천 티어업 팁**: 승률을 올리기 위한 핵심 조언 2~3가지

- LaTeX 수식 문법 사용 금지.
"""
        return self._generate(prompt)

    def analyze_ingame(self, my_champ: str, vs_champ: str, my_team: list, enemy_team: list) -> str:
        """인게임 매치업 및 승리 플랜 분석"""
        prompt = f"""
리그 오브 레전드 AI 코치로서 현재 진행 중인 게임을 분석하세요.

[내 챔피언]: {my_champ}
[상대 라이너 챔피언]: {vs_champ}
[우리 팀 조합]: {', '.join(my_team)}
[상대 팀 조합]: {', '.join(enemy_team)}

다음 항목을 핵심 위주로 명확하게 작성하세요:
1. **라인전 맞대결 핵심 팁**: {my_champ} vs {vs_champ} 라인전 핵심 딜교환 및 스킬 활용법
2. **주의해야 할 상대 주요 챔피언/스킬**: 한타나 로밍 시 경계해야 할 요소
3. **팀 승리 플랜**: 조합 특성을 고려한 한타 또는 운영 방향성

- LaTeX 수식 표현($, \\Large 등) 금지.
- 디스코드에 출력하기 좋은 핵심 위주 마크다운 형식 사용.
"""
        return self._generate(prompt)

    def get_champion_tip(self, my_champ: str, vs_champ: str) -> str:
        """1v1 챔피언 맞대결 팁 조회"""
        prompt = f"""
리그 오브 레전드 AI 코치로서 챔피언 1v1 맞대결 팁을 제시하세요.

[내 챔피언]: {my_champ}
[상대 챔피언]: {vs_champ}

다음 항목을 마크다운 형식으로 작성하세요:
1. **라인전 주도권 및 상성 요약**: 상성 우위 및 초기 라인 관리법
2. **딜교환 핵심 팁**: 주요 스킬 타이밍 및 딜교환 콤보
3. **주의해야 할 상대 핵심 스킬**: 피하거나 의식해야 할 스킬

- LaTeX 수식 표현 금지.
- 디스코드용 마크다운 형식 사용.
"""
        return self._generate(prompt)
