"""Gemini 기반 League of Legends 코칭 분석 모듈."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from typing import Any

from cachetools import TTLCache
from google import genai
from google.genai import types


@dataclass(frozen=True)
class CoachingReport:
    """디스코드 Embed와 코칭 스레드가 공유하는 분석 결과."""

    one_liner: str
    markdown: str


@dataclass
class CoachingSession:
    """세션 원본 문맥과 길이가 제한된 사용자·모델 대화 이력."""

    system_instruction: str
    history: list[Any]
    turn_count: int = 0


def clean_latex(text: str) -> str:
    """Discord 출력에 부적절한 LaTeX 문법과 과도한 공백을 정리한다."""
    if not text:
        return ""
    text = re.sub(r"\\(?:Large|large|huge|Huge|small|tiny|bf|it)\b", "", text)
    text = re.sub(r"\$\$?", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class GeminiAnalyzer:
    """Gemini API를 이용한 전적 분석·코칭 대화 서비스."""

    DEFAULT_MODEL = "gemini-3.6-flash"
    MAX_DISCORD_TEXT = 3_700
    MAX_SESSION_TURNS = 12
    MAX_SESSION_HISTORY_MESSAGES = MAX_SESSION_TURNS * 2

    def __init__(self, api_key: str | None, model_name: str | None = None) -> None:
        self.api_key = (api_key or "").strip()
        self.model_name = (model_name or self.DEFAULT_MODEL).strip()
        self.client = genai.Client(api_key=self.api_key) if self.api_key else None
        self.sessions: TTLCache[int, CoachingSession] = TTLCache(maxsize=500, ttl=7200)
        self._lock = threading.Lock()

    def _ensure_client(self) -> Any:
        if self.client is None:
            raise RuntimeError("GEMINI_API_KEY 환경 변수가 설정되지 않았습니다.")
        return self.client

    def _generate(self, prompt: str, *, max_output_tokens: int = 1_100) -> str:
        """최신 google-genai SDK의 Gemini 모델 호출을 하나의 경로로 통합한다."""
        client = self._ensure_client()
        try:
            response = client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.45,
                    max_output_tokens=max_output_tokens,
                ),
            )
            text = clean_latex((getattr(response, "text", None) or "").strip())
            if not text:
                raise RuntimeError("Gemini가 빈 응답을 반환했습니다.")
            return text
        except Exception as error:
            print(f"[GeminiAnalyzer] 생성 실패: {error}", flush=True)
            raise RuntimeError("AI 코칭 리포트를 생성하지 못했습니다. 잠시 후 다시 시도해 주세요.") from error

    @staticmethod
    def _extract_one_liner(text: str) -> tuple[str, str]:
        """프롬프트의 고정 첫 줄을 Embed의 짧은 피드백으로 분리한다."""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return "분석 결과를 생성하지 못했습니다.", "분석 결과를 생성하지 못했습니다."

        first_line = lines[0]
        match = re.match(r"(?:\*\*)?한줄 피드백(?:\*\*)?\s*[:：]\s*(.+)", first_line)
        one_liner = match.group(1).strip() if match else first_line.lstrip("#-• ").strip()
        one_liner = re.sub(r"\*+", "", one_liner)
        one_liner = one_liner[:180].rstrip()

        markdown = text
        if match:
            markdown = "\n".join(lines[1:]).strip()
        if not markdown:
            markdown = text
        return one_liner, markdown[: GeminiAnalyzer.MAX_DISCORD_TEXT]

    @staticmethod
    def _compact_json(data: dict[str, Any]) -> str:
        """모델에 전달하는 지표를 일관된 JSON으로 직렬화한다."""
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    def analyze_recent_performance(
        self,
        summoner_name: str,
        performance: dict[str, Any],
        rank_text: str,
        queue_name: str,
    ) -> CoachingReport:
        """최근 5경기의 정량 지표를 토대로 개인화된 전적 코칭을 생성한다."""
        prompt = f"""
너는 리그 오브 레전드 전문 개인 코치다. 아래 전적은 '{summoner_name}'의 최근 {queue_name} 경기 데이터다.
반드시 주어진 데이터에서 확인되는 사실만 근거로 삼고, 확인할 수 없는 스킬 적중률·와드 위치·교전 장면을 지어내지 마라.

[티어]
{rank_text}

[전적 요약 JSON]
{self._compact_json(performance)}

다음 형식으로 한국어 코칭 리포트를 작성해라.
첫 줄은 반드시 `한줄 피드백: `으로 시작하며, 80자 이내의 실전적인 핵심 진단을 쓴다.
그 다음에는 `## 총평`, `## 잘한 점`, `## 다음 3경기 행동 목표` 제목을 각각 하나씩 사용한다.
`다음 3경기 행동 목표`에는 실행 가능한 항목을 정확히 3개 작성한다.
최근 경기 수가 적다면 표본이 작다는 점을 분명히 밝혀라.
마크다운은 디스코드에서 바로 읽기 좋게 작성하고 LaTex 수식, 과도한 인사말, 면책문구는 쓰지 마라.
""".strip()
        text = self._generate(prompt)
        one_liner, markdown = self._extract_one_liner(text)
        return CoachingReport(one_liner=one_liner, markdown=markdown)

    def analyze_match_history(self, summoner_name: str, match_summary_text: str) -> str:
        """기존 호출 코드와의 호환을 위한 전적 분석 래퍼."""
        prompt = f"""
너는 리그 오브 레전드 전문 AI 코치다. 소환사 '{summoner_name}'의 최근 전적을 분석해라.

[전적 요약]
{match_summary_text}

첫 줄을 `한줄 피드백: ` 형식으로 쓰고, 이어서 `## 총평`, `## 강점`, `## 개선 목표`를 사용해
근거 중심의 짧고 실전적인 한국어 코칭을 작성해라. LaTex 수식은 사용하지 마라.
""".strip()
        return self._generate(prompt)

    def analyze_match_review(self, summoner_name: str, review_data: dict[str, Any]) -> CoachingReport:
        """Match-V5 타임라인 기반의 경기 종료 후 복기 리포트를 생성한다."""
        prompt = f"""
너는 리그 오브 레전드 전문 코치다. '{summoner_name}'의 단일 경기 복기 데이터를 분석해라.
이 데이터는 Riot Match-V5의 결과 및 이벤트 타임라인에서 추출한 것이며, 영상 장면이나 스킬 적중 여부는 포함하지 않는다.
따라서 없는 상황을 추정하지 말고, 사망 시점·처치·오브젝트·아이템 이벤트와 최종 지표만 근거로 조언해라.

[경기 복기 JSON]
{self._compact_json(review_data)}

다음 형식을 지켜 한국어로 작성해라.
첫 줄: `한줄 피드백: ` 뒤에 가장 중요한 복기 포인트를 80자 이내로 작성한다.
이후 `## 경기 요약`, `## 다시 볼 타이밍`, `## 다음 게임 적용` 제목을 사용한다.
`다시 볼 타이밍`에서는 제공된 이벤트 중 우선순위가 높은 시점을 최대 3개 제시한다. 타임라인이 없으면 그 한계를 명시하고 결과 지표 중심으로 쓴다.
`다음 게임 적용`은 구체적인 실행 항목 3개로 끝낸다. LaTex 수식은 쓰지 마라.
""".strip()
        text = self._generate(prompt)
        one_liner, markdown = self._extract_one_liner(text)
        return CoachingReport(one_liner=one_liner, markdown=markdown)

    def ask_general(self, question: str) -> str:
        """일반 멘션 질문에 대해 간결한 LoL 코칭 답변을 생성한다."""
        prompt = f"""
너는 리그 오브 레전드(LoL) 전문 AI 코치다. 다음 질문에 핵심 위주로 정확히 답하라.
패치별 수치처럼 최신성이 중요한 정보는 단정하지 말고 사용자가 현재 게임 클라이언트에서 확인할 수 있도록 안내하라.

[질문]
{question}

한국어 마크다운으로 350자 이내에 답하고 LaTex 수식은 사용하지 마라.
""".strip()
        return self._generate(prompt, max_output_tokens=600)

    def get_champion_tip(self, my_champ: str, vs_champ: str) -> str:
        """특정 1:1 매치업의 라인전 중심 팁을 생성한다."""
        prompt = f"""
너는 리그 오브 레전드 전문 AI 코치다. '{my_champ}' 대 '{vs_champ}'의 라인전 코칭을 작성해라.
패치에 따라 달라질 수 있는 승률이나 수치로 상성을 단정하지 말고 스킬 교환, 웨이브, 시야, 레벨 타이밍 중심으로 설명해라.

`## 라인전 핵심`, `## 딜교환`, `## 위험 신호`의 3개 제목을 쓰고 각 섹션을 짧게 작성해라.
LaTex 수식은 사용하지 마라.
""".strip()
        return self._generate(prompt, max_output_tokens=750)

    def analyze_ingame(self, my_champ: str, my_team: list[str], enemy_team: list[str]) -> str:
        """진행 중 게임의 공개 로스터로 조합·운영 조언을 생성한다."""
        prompt = f"""
너는 리그 오브 레전드 전문 AI 코치다. 진행 중인 게임의 공개 챔피언 조합을 바탕으로 조언해라.
적의 쿨다운, 위치, 시야처럼 플레이어가 게임 화면에서 알 수 없는 정보를 제공하거나, 행동을 강요하는 표현은 쓰지 마라.

[내 챔피언] {my_champ}
[우리 팀] {', '.join(my_team)}
[상대 팀] {', '.join(enemy_team)}

`## 내 역할`, `## 조합상 주의점`, `## 한타·오브젝트 플랜`을 중심으로 짧고 실전적인 한국어 코칭을 작성해라.
""".strip()
        return self._generate(prompt, max_output_tokens=800)

    def start_coaching_session(
        self,
        session_id: int,
        summoner_name: str,
        match_summary: str,
        initial_analysis: str,
    ) -> None:
        """분석 결과를 시스템 문맥으로 고정한 제한형 코칭 세션을 연다."""
        self._ensure_client()
        system_instruction = f"""
너는 리그 오브 레전드 AI 코치다. 지금 '{summoner_name}'와 디스코드 코칭 스레드에서 대화한다.

[전적 또는 경기 복기 데이터]
{match_summary[:8000]}

[최초 리포트]
{initial_analysis[:5000]}

후속 질문에는 위 데이터에서 확인되는 사실을 우선 근거로 삼아, 짧고 실전적으로 답한다.
알 수 없는 장면·수치·플레이를 사실처럼 말하지 않는다. LaTex 수식은 사용하지 않는다.
""".strip()
        with self._lock:
            self.sessions[session_id] = CoachingSession(system_instruction=system_instruction, history=[])
        print(f"[GeminiAnalyzer] 제한형 코칭 세션 생성 완료: channel_id={session_id}", flush=True)

    def has_session(self, session_id: int) -> bool:
        with self._lock:
            return session_id in self.sessions

    def continue_coaching_session(self, session_id: int, user_message: str) -> str:
        """제한된 최근 대화 이력과 고정 분석 문맥을 사용해 후속 질문에 답한다."""
        with self._lock:
            session = self.sessions.get(session_id)
            if session is not None:
                history = list(session.history[-self.MAX_SESSION_HISTORY_MESSAGES :])
                system_instruction = session.system_instruction
        if session is None:
            return self.ask_general(user_message)

        client = self._ensure_client()
        try:
            chat = client.chats.create(
                model=self.model_name,
                config=types.GenerateContentConfig(system_instruction=system_instruction),
                history=history,
            )
            response = chat.send_message(user_message)
            text = clean_latex((getattr(response, "text", None) or "").strip())
            if not text:
                raise RuntimeError("Gemini가 빈 응답을 반환했습니다.")

            user_content = types.Content(role="user", parts=[types.Part.from_text(text=user_message)])
            model_content = types.Content(role="model", parts=[types.Part.from_text(text=text)])
            with self._lock:
                current_session = self.sessions.get(session_id)
                if current_session is not None:
                    current_session.history.extend([user_content, model_content])
                    current_session.history = current_session.history[-self.MAX_SESSION_HISTORY_MESSAGES :]
                    current_session.turn_count += 1
            return text[: self.MAX_DISCORD_TEXT]
        except Exception as error:
            print(f"[GeminiAnalyzer] 코칭 대화 실패: {error}", flush=True)
            return "AI 코치 답변을 생성하지 못했습니다. 질문을 조금 바꾸어 다시 시도해 주세요."

    def continue_coaching(self, session_id: int, user_message: str) -> str:
        """이전 메서드명을 사용하는 호출부와의 호환 래퍼."""
        return self.continue_coaching_session(session_id, user_message)
""
