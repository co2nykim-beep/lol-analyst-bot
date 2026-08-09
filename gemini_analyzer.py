"""
Gemini 3.6 Flash 분석기 (수정판 v3 - 스레드 안전성 + 타임아웃 처리)

실전 운영 문제 해결:
1. [추가] threading.Lock으로 TTLCache 동시성 보호 (race condition 방지)
2. [변경] continue_coaching()에서 세션 만료 감지 시 사용자에게 명확한 안내 메시지 반환
3. [설명] start_coaching_session()은 여전히 동기이지만, bot.py에서 asyncio.to_thread로
   감싸서 메인 이벤트 루프 차단을 방지합니다.
"""
import re
import threading

from cachetools import TTLCache
from google import genai
from google.genai import types


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
        # 스레드 ID -> Chat 세션. 2시간 미사용 시 자동 만료.
        self.sessions: TTLCache = TTLCache(maxsize=500, ttl=7200)
        # 🔒 TTLCache는 스레드 세이프하지 않으므로 Lock으로 보호
        self._lock = threading.Lock()

    def _generate(self, prompt: str) -> str:
        """Gemini API 호출 및 응답 처리 (단발성 요청용)"""
        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
            )
            return clean_latex((response.text or "").strip())
        except Exception as e:
            print(f"[GeminiAnalyzer] 생성 실패: {e}", flush=True)
            return f"⚠️ AI 생성 실패: {e}"

    def analyze_match_history(self, summoner_name: str, match_summary_text: str) -> str:
        """최근 전적 및 피드백 분석 (match_summary_text는 실제 KDA/챔피언/승패가 포함된 요약이어야 함)"""
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

    def analyze_ingame(self, my_champ: str, my_team: list, enemy_team: list) -> str:
        """인게임 팀 조합 분석 (라인별 상대 매칭 정보는 Riot API가 제공하지 않아 팀 조합 기준으로 분석)"""
        prompt = f"""
리그 오브 레전드 AI 코치로서 현재 진행 중인 게임을 분석하세요.

[내 챔피언]: {my_champ}
[우리 팀 조합]: {', '.join(my_team)}
[상대 팀 조합]: {', '.join(enemy_team)}

다음 항목을 핵심 위주로 명확하게 작성하세요:
1. **{my_champ} 운영 팁**: 이번 조합에서 {my_champ}가 집중해야 할 역할과 타이밍
2. **경계해야 할 상대 챔피언**: 상대 팀 조합에서 특히 주의해야 할 챔피언과 이유
3. **팀 승리 플랜**: 두 조합의 특성을 고려한 한타/운영 방향성

- LaTeX 수식 표현($, \\Large 등) 금지.
- 디스코드에 출력하기 좋은 핵심 위주 마크다운 형식 사용.
"""
        return self._generate(prompt)

    def get_champion_tip(self, my_champ: str, vs_champ: str) -> str:
        """1v1 챔피언 맞대결 팁 조회 (사용자가 직접 입력한 챔피언이므로 그대로 사용 가능)"""
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

    # ---- /롤 전적 스레드에서 이어지는 1:1 코칭 대화 ----
    def start_coaching_session(
        self, session_id, summoner_name: str, match_summary: str, initial_analysis: str
    ):
        """
        전적 분석 스레드가 열릴 때 호출. Gemini 멀티턴 채팅 세션을 만들어 저장합니다.
        
        ⚠️ 주의: 이 메서드는 동기식(sync)이므로 네트워크 I/O 블록이 발생합니다.
        bot.py에서 반드시 asyncio.to_thread()로 감싸서 호출하세요.
        """
        system_instruction = (
            f"너는 리그 오브 레전드 AI 코치야. 지금 '{summoner_name}' 소환사와 "
            "디스코드 스레드에서 방금 보낸 전적 분석에 대해 1:1로 대화하고 있어.\n\n"
            f"[분석 대상 전적 데이터]\n{match_summary}\n\n"
            f"[방금 사용자에게 보여준 최초 분석]\n{initial_analysis}\n\n"
            "이후 사용자의 후속 질문에는 위 데이터를 근거로 짧고 실전적으로 답변해. "
            "LaTeX 수식 표현은 쓰지 마."
        )
        try:
            chat = self.client.chats.create(
                model=self.model_name,
                config=types.GenerateContentConfig(system_instruction=system_instruction),
            )
            # 🔒 Lock으로 sessions 쓰기 보호
            with self._lock:
                self.sessions[session_id] = chat
            print(f"[GeminiAnalyzer] 코칭 세션 생성 완료: thread_id={session_id}", flush=True)
        except Exception as e:
            print(f"[GeminiAnalyzer] 코칭 세션 생성 실패: {e}", flush=True)

    def continue_coaching(self, session_id, user_message: str) -> str:
        """
        코칭 스레드에서 사용자가 후속 메시지를 보낼 때 호출.
        
        ⚠️ 주의: 이 메서드는 동기식(sync)이므로 네트워크 I/O 블록이 발생합니다.
        bot.py의 on_message에서 반드시 asyncio.to_thread()로 감싸서 호출하세요.
        """
        # 🔒 Lock으로 sessions 읽기 보호
        with self._lock:
            chat = self.sessions.get(session_id)
        
        if chat is None:
            return (
                "⚠️ 대화 세션이 만료되었습니다 (2시간 미사용 시 자동 정리).\n"
                "다시 피드백을 받고 싶다면 `/롤 전적`을 다시 실행해주세요!"
            )
        try:
            response = chat.send_message(user_message)
            return clean_latex((response.text or "").strip())
        except Exception as e:
            print(f"[GeminiAnalyzer] 코칭 대화 실패: {e}", flush=True)
            return f"⚠️ 답변 생성에 실패했습니다: {e}"

    # ---- 에러 트레이스백 AI 진단 ----
    def analyze_error(self, error_log: str) -> str:
        """@bot.tree.error에서 예외 발생 시 관리자 DM용 AI 진단을 생성합니다."""
        prompt = f"""
너는 파이썬(discord.py) 전문 디버깅 어시스턴트야. 아래는 실제 발생한 예외 트레이스백이야.

[트레이스백]
{error_log[:3000]}

다음을 마크다운으로 간결하게 작성해:
1. **원인**: 어떤 코드/줄에서 왜 발생했는지 1~2문장
2. **해결 방법**: 구체적으로 어떤 코드를 어떻게 고쳐야 하는지
3. **재발 방지 팁**: 비슷한 에러를 막을 수 있는 팁 (있다면)
"""
        return self._generate(prompt)
