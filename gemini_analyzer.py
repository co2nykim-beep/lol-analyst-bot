import os
import re
import asyncio
from google import genai
from groq import Groq

def clean_latex(text: str) -> str:
    text = text.replace(r"\ge", " 이상 ").replace(r"\le", " 이하 ")
    text = text.replace(r"\rightarrow", " -> ").replace(r"\leftarrow", " <- ")
    text = re.sub(r'\$([^\$]+)\$', r'\1', text)
    text = text.replace('$', '')
    return text

class GeminiAnalyzer:
    def __init__(self, api_key: str, model_name: str = "gemini-3.6-flash"):
        self.client = genai.Client(api_key=api_key)
        self.model_name = model_name
        groq_key = os.getenv("GROQ_API_KEY")
        self.groq_client = Groq(api_key=groq_key) if groq_key else None

    async def analyze_match_history(self, game_name: str, tag_line: str, match_summary: list) -> str:
        prompt = f"""
당신은 전설적인 리그 오브 레전드 전문 AI 코치입니다.
소환사 '{game_name}#{tag_line}' 님의 최근 {len(match_summary)}경기 기록을 분석하고 종합 코칭 리포트를 작성해 주세요.

[최근 경기 데이터]
{match_summary}

[작성 지침]
1. 공백 포함 **700자 이내**로 완결된 문장으로 작성하세요.
2. LaTeX 수식 기호($, \\ge 등)를 절대 쓰지 마세요.
3. 승률 평가, 플레이 스타일 핵심 패턴, 뇌절 방지 개선책 1가지를 제시하세요.
"""
        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt
            )
            return clean_latex(response.text.strip())
        except Exception as e:
            return f"⚠️ 분석 생성 중 오류가 발생했습니다: {e}"

    async def get_champion_tip(self, my_champ: str, vs_champ: str = "") -> str:
        """3번 기능: 챔피언 및 상대법 전용 코칭"""
        vs_info = f"VS {vs_champ} 상대법" if vs_champ else "라인전 및 운영 핵심"
        prompt = f"""
리그 오브 레전드 전문 AI 코치로서 [{my_champ}] 챔피언의 {vs_info}을 공략해 주세요.

[필수 포함 항목]
1. 핵심 룬 및 라인전 딜교환 핵심 메커니즘
2. {f'{vs_champ} 상대 핵심 팁 및 스킬 활용법' if vs_champ else '한타 역할 및 핵심 아이템'}
3. 주의해야 할 뇌절 포인트

[작성 지침]
- LaTeX 수식 표현($, \\ge 등) 금지. 700자 이내 명확하고 실전적인 스타일로 작성하세요.
"""
        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt
            )
            return clean_latex(response.text.strip())
        except Exception as e:
            return f"⚠️ 팁 생성 오류: {e}"

    async def answer_general_question(self, user_question: str) -> str:
        prompt = f"""
사용자 질문: {user_question}
- LoL 전문 AI 코치로서 핵심을 명확하게 답변해 주세요.
- LaTeX 수식 표현($, \\ge 등) 금지.
"""
        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt
            )
            return clean_latex(response.text.strip())
        except Exception as e:
            return f"⚠️ 답변 생성 오류: {e}"