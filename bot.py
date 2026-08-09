import asyncio
import os
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from riot_client import RiotClient, RiotAPIError
from gemini_analyzer import GeminiAnalyzer

# --- Render 헬스체크 웹서버 ---
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass


def keep_alive():
    port = int(os.environ.get("PORT", 10000))
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()


keep_alive()
# ----------------------------

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
RIOT_KEY = os.getenv("RIOT_API_KEY")
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
SYNC_COMMANDS = os.getenv("SYNC_COMMANDS", "false").lower() == "true"

riot_client = RiotClient(api_key=RIOT_KEY)
gemini_analyzer = GeminiAnalyzer(api_key=GEMINI_KEY, model_name=MODEL_NAME)


def build_embed(title: str, description: str, color: discord.Color, rank_text: str | None = None) -> discord.Embed:
    if description is None:
        description = "분석 결과를 불러올 수 없습니다."
    else:
        description = str(description)

    if len(description) > 4000:
        description = description[:4000] + "\n\n...(내용이 길어 일부 생략됨)"

    embed = discord.Embed(title=str(title), description=description, color=color)
    if rank_text:
        embed.add_field(name="🏆 현재 솔로랭크", value=str(rank_text), inline=False)
    return embed


def create_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True

    bot = commands.Bot(command_prefix="!", intents=intents)
    lol_group = app_commands.Group(name="롤", description="LoL AI 코치 분석 명령어 모음")
    bot.tree.add_command(lol_group)

    @bot.event
    async def on_ready():
        print(f"=== {bot.user.name} 봇 연결 성공! ===", flush=True)
        if SYNC_COMMANDS:
            try:
                synced = await bot.tree.sync()
                print(f"등록된 슬래시 명령어 개수: {len(synced)}개", flush=True)
            except Exception as e:
                print(f"명령어 동기화 실패: {e}", flush=True)

    @bot.tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        error_log = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        print(f"⚠️ 시스템 오류 감지:\n{error_log}", flush=True)

        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("⚠️ 처리 중 오류가 발생했습니다.", ephemeral=True)
            else:
                await interaction.followup.send("⚠️ 처리 중 오류가 발생했습니다.", ephemeral=True)
        except discord.errors.NotFound:
            pass

    @lol_group.command(name="전적", description="소환사의 최근 전적 데이터를 AI가 분석하고 1:1 대화 스레드를 개설합니다.")
    @app_commands.describe(game_name="소환사 이름", tag_line="태그 (예: KR1)")
    async def match_analysis(interaction: discord.Interaction, game_name: str, tag_line: str):
        # 3초 타임아웃 방지를 위해 최우선 실행
        await interaction.response.defer(thinking=True)

        try:
            account = await riot_client.get_account_by_riot_id(game_name, tag_line)
            puuid = account.get("puuid")
            if not puuid:
                await interaction.followup.send("❌ 소환사 정보를 찾을 수 없습니다.")
                return

            rank_text = await _get_rank_text(puuid)
            match_ids = await riot_client.get_recent_matches(puuid, count=5)
            if not match_ids:
                await interaction.followup.send("⚠️ 최근 게임 기록이 없습니다.")
                return

            lines = []
            for match_id in match_ids:
                detail = await riot_client.get_match_detail(match_id)
                p = next((pp for pp in detail["info"]["participants"] if pp.get("puuid") == puuid), None)
                if not p:
                    continue
                result = "승리" if p.get("win") else "패배"
                cs = p.get("totalMinionsKilled", 0) + p.get("neutralMinionsKilled", 0)
                lines.append(
                    f"- {p.get('championName', '미상')} ({p.get('teamPosition', '?')}): "
                    f"{p.get('kills', 0)}/{p.get('deaths', 0)}/{p.get('assists', 0)} KDA, "
                    f"딜량 {p.get('totalDamageDealtToChampions', 0):,}, CS {cs}, {result}"
                )

            summary_text = f"현재 솔로랭크 티어: {rank_text}\n\n최근 {len(lines)}경기 기록:\n" + "\n".join(lines)

            raw_res = await asyncio.to_thread(
                gemini_analyzer.analyze_match_history, f"{game_name}#{tag_line}", summary_text
            )
            analysis_res = str(raw_res) if raw_res is not None else "분석 결과를 생성하지 못했습니다."

            embed = build_embed(
                title=f"📊 {game_name}#{tag_line} AI 전적 분석 결과",
                description=analysis_res,
                color=discord.Color.blue(),
                rank_text=rank_text,
            )

            msg = await interaction.followup.send(embed=embed, wait=True)

            thread = await msg.create_thread(
                name=f"💬 {game_name} AI 코치 1:1 피드백 채널",
                auto_archive_duration=60,
            )

            await asyncio.to_thread(
                gemini_analyzer.start_coaching_session,
                session_id=thread.id,
                summoner_name=f"{game_name}#{tag_line}",
                match_summary=summary_text,
                initial_analysis=analysis_res,
            )

            await thread.send(
                f"👋 안녕하세요 **{game_name}**님! 전적 피드백 결과에 대해 궁금한 점이 있다면 무엇이든 편하게 물어보세요."
            )

        except RiotAPIError as e:
            await interaction.followup.send(f"⚠️ 라이엇 API 오류: {e.message}")
        except Exception as e:
            await interaction.followup.send(f"⚠️ 처리 도중 오류가 발생했습니다: {e}")

    async def _get_rank_text(puuid: str) -> str:
        try:
            entries = await riot_client.get_league_entries(puuid)
        except RiotAPIError:
            return "조회 실패"
        solo = next((e for e in entries if e.get("queueType") == "RANKED_SOLO_5x5"), None)
        if not solo:
            return "언랭"
        return f"{solo['tier']} {solo['rank']} {solo['leaguePoints']}LP ({solo['wins']}승 {solo['losses']}패)"

    return bot


async def main():
    if not DISCORD_TOKEN:
        print("❌ DISCORD_BOT_TOKEN 환경 변수가 설정되지 않았습니다.", flush=True)
        return

    retry_delay = 120
    while True:
        current_bot = create_bot()
        try:
            async with current_bot:
                await current_bot.start(DISCORD_TOKEN)
            break
        except discord.errors.HTTPException as e:
            if e.status in (429, 502, 504):
                print(
                    f"⚠️ Discord API HTTP {e.status} (Cloudflare IP 차단) 감지. "
                    f"{retry_delay}초 동안 대기 후 새 세션으로 재시도합니다...",
                    flush=True,
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 600)
            else:
                print(f"❌ Discord HTTP 오류 발생: {e}", flush=True)
                await asyncio.sleep(30)
        except Exception as e:
            print(f"❌ 봇 실행 중 예외 발생: {e}", flush=True)
            await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(main())
