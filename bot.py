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

# 큐 ID 매핑
QUEUE_TYPES = {
    420: "솔랭",
    440: "자랭",
    450: "칼바람",
    400: "일반",
    490: "빠른대전",
}

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
        embed.add_field(name="🏆 현재 티어 현황", value=str(rank_text), inline=False)
    return embed


def create_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True

    bot = commands.Bot(command_prefix="!", intents=intents)
    lol_group = app_commands.Group(name="롤", description="LoL AI 코치 분석 명령어 모음")

    @bot.event
    async def on_ready():
        print(f"=== {bot.user.name} 봇 연결 성공! ===", flush=True)
        try:
            synced = await bot.tree.sync()
            print(f"동기화 완료된 슬래시 명령어 개수: {len(synced)}개", flush=True)
            for cmd in synced:
                print(f" - 등록 명령어: /{cmd.name}", flush=True)
        except Exception as e:
            print(f"⚠️ 명령어 동기화 실패: {e}", flush=True)

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

    async def _get_rank_text(puuid: str) -> str:
        try:
            entries = await riot_client.get_league_entries(puuid)
        except RiotAPIError:
            return "조회 실패"
        solo = next((e for e in entries if e.get("queueType") == "RANKED_SOLO_5x5"), None)
        flex = next((e for e in entries if e.get("queueType") == "RANKED_FLEX_SR"), None)

        solo_str = f"{solo['tier']} {solo['rank']} ({solo['leaguePoints']}LP, {solo['wins']}승 {solo['losses']}패)" if solo else "언랭"
        flex_str = f"{flex['tier']} {flex['rank']} ({flex['leaguePoints']}LP, {flex['wins']}승 {flex['losses']}패)" if flex else "언랭"

        return f"솔로랭크: {solo_str} | 자유랭크: {flex_str}"

    # 1. /롤 전적
    @lol_group.command(name="전적", description="소환사의 솔랭/자랭 선택 전적(최근 10경기)을 분석하고 1:1 코칭 스레드를 개설합니다.")
    @app_commands.describe(
        game_name="소환사 이름",
        tag_line="태그 (예: KR1)",
        queue_type="분석할 큐 종류를 선택하세요 (솔로랭크 / 자유랭크)"
    )
    @app_commands.choices(queue_type=[
        app_commands.Choice(name="솔로랭크", value="420"),
        app_commands.Choice(name="자유랭크", value="440")
    ])
    async def match_analysis(
        interaction: discord.Interaction, 
        game_name: str, 
        tag_line: str, 
        queue_type: app_commands.Choice[str]
    ):
        await interaction.response.defer(thinking=True)

        try:
            account = await riot_client.get_account_by_riot_id(game_name, tag_line)
            puuid = account.get("puuid")
            if not puuid:
                await interaction.followup.send("❌ 소환사 정보를 찾을 수 없습니다.")
                return

            rank_text = await _get_rank_text(puuid)
            target_queue_id = int(queue_type.value)
            target_queue_name = queue_type.name

            try:
                match_ids = await riot_client.get_recent_matches(puuid, count=10, queue=target_queue_id)
            except TypeError:
                match_ids = await riot_client.get_recent_matches(puuid, count=20)

            if not match_ids:
                await interaction.followup.send(f"⚠️ 최근 {target_queue_name} 게임 기록이 없습니다.")
                return

            lines = []
            for match_id in match_ids:
                detail = await riot_client.get_match_detail(match_id)
                q_id = detail["info"].get("queueId", 0)
                
                if q_id != target_queue_id:
                    continue

                p = next((pp for pp in detail["info"]["participants"] if pp.get("puuid") == puuid), None)
                if not p:
                    continue
                
                result = "승리" if p.get("win") else "패배"
                cs = p.get("totalMinionsKilled", 0) + p.get("neutralMinionsKilled", 0)
                lines.append(
                    f"- [{target_queue_name}] {p.get('championName', '미상')} ({p.get('teamPosition', '?')}): "
                    f"{p.get('kills', 0)}/{p.get('deaths', 0)}/{p.get('assists', 0)} KDA, "
                    f"딜량 {p.get('totalDamageDealtToChampions', 0):,}, CS {cs}, {result}"
                )
                
                if len(lines) >= 10:
                    break

            if not lines:
                await interaction.followup.send(f"⚠️ 최근 기록 중 {target_queue_name} 매치를 찾을 수 없습니다.")
                return

            summary_text = (
                f"분석 요청 큐: {target_queue_name}\n"
                f"전체 티어 현황: {rank_text}\n\n"
                f"최근 {target_queue_name} {len(lines)}경기 상세 데이터:\n" + "\n".join(lines)
            )

            raw_res = await asyncio.to_thread(
                gemini_analyzer.analyze_match_history, f"{game_name}#{tag_line}", summary_text
            )
            analysis_res = str(raw_res) if raw_res is not None else "분석 결과를 생성하지 못했습니다."

            embed = build_embed(
                title=f"📊 {game_name}#{tag_line} [{target_queue_name}] AI 전적 분석 결과",
                description=analysis_res,
                color=discord.Color.blue(),
                rank_text=rank_text,
            )

            msg = await interaction.followup.send(embed=embed, wait=True)

            target_session_id = None
            if interaction.guild and hasattr(msg, "create_thread") and isinstance(interaction.channel, (discord.TextChannel, discord.ForumChannel)):
                try:
                    thread = await msg.create_thread(
                        name=f"💬 {game_name} [{target_queue_name}] AI 코치 1:1 채널",
                        auto_archive_duration=60,
                    )
                    target_session_id = thread.id
                    await thread.send(
                        f"👋 안녕하세요 **{game_name}**님! **{target_queue_name}** 전적 피드백에 대해 궁금한 점이 있다면 명령어 없이 편하게 질문해주세요!"
                    )
                except Exception as thread_err:
                    print(f"⚠️ 스레드 생성 실패: {thread_err}", flush=True)
                    target_session_id = interaction.channel_id
            else:
                target_session_id = interaction.channel_id

            await asyncio.to_thread(
                gemini_analyzer.start_coaching_session,
                session_id=target_session_id,
                summoner_name=f"{game_name}#{tag_line}",
                match_summary=summary_text,
                initial_analysis=analysis_res,
            )

        except RiotAPIError as e:
            await interaction.followup.send(f"⚠️ 라이엇 API 오류: {e.message}")
        except Exception as e:
            await interaction.followup.send(f"⚠️ 처리 도중 오류가 발생했습니다: {e}")

    # 2. /롤 팁 (그룹 명령어)
    @lol_group.command(name="팁", description="특정 챔피언 간의 상대법 및 라인전 팁을 조회합니다.")
    @app_commands.describe(
        my_champ="내 챔피언 이름 (예: 쉔, 밀리오)",
        vs_champ="상대 챔피언 이름 (예: 카밀, 룰루)"
    )
    async def champion_tip_group(
        interaction: discord.Interaction,
        my_champ: str,
        vs_champ: str
    ):
        await interaction.response.defer(thinking=True)
        try:
            raw_res = await asyncio.to_thread(
                gemini_analyzer.get_champion_tip, my_champ, vs_champ
            )
            embed = build_embed(
                title=f"⚔️ {my_champ} vs {vs_champ} 라인전 코칭 팁",
                description=str(raw_res),
                color=discord.Color.green(),
            )
            await interaction.followup.send(embed=embed)
        except Exception as e:
            await interaction.followup.send(f"⚠️ 팁 생성 중 오류가 발생했습니다: {e}")

    # 3. /팁 (단독 명령어 호환용)
    @bot.tree.command(name="팁", description="특정 챔피언 간의 상대법 및 라인전 팁을 조회합니다.")
    @app_commands.describe(
        my_champ="내 챔피언 이름 (예: 쉔, 밀리오)",
        vs_champ="상대 챔피언 이름 (예: 카밀, 룰루)"
    )
    async def champion_tip_standalone(
        interaction: discord.Interaction,
        my_champ: str,
        vs_champ: str
    ):
        await interaction.response.defer(thinking=True)
        try:
            raw_res = await asyncio.to_thread(
                gemini_analyzer.get_champion_tip, my_champ, vs_champ
            )
            embed = build_embed(
                title=f"⚔️ {my_champ} vs {vs_champ} 라인전 코칭 팁",
                description=str(raw_res),
                color=discord.Color.green(),
            )
            await interaction.followup.send(embed=embed)
        except Exception as e:
            await interaction.followup.send(f"⚠️ 팁 생성 중 오류가 발생했습니다: {e}")

    # 그룹 명령어 동기화 트리 추가
    bot.tree.add_command(lol_group)

    @bot.event
    async def on_message(message: discord.Message):
        if message.author.bot:
            return

        channel = message.channel
        session_id = channel.id

        is_coaching_thread = isinstance(channel, discord.Thread) and (
            channel.owner_id == bot.user.id or "AI 코치" in channel.name
        )
        has_active_session = gemini_analyzer.has_session(session_id)

        # 1. 기존 코칭 세션 내 대화
        if is_coaching_thread or has_active_session:
            async with channel.typing():
                try:
                    reply_text = await asyncio.to_thread(
                        gemini_analyzer.continue_coaching_session,
                        session_id=session_id,
                        user_message=message.content,
                    )
                    await message.reply(reply_text)
                except Exception as e:
                    await message.reply(f"⚠️ 답변 생성 중 오류가 발생했습니다: {e}")
            return

        # 2. 일반 채널 direct 멘션 질의
        if bot.user.mentioned_in(message) and not message.mention_everyone:
            clean_content = message.content.replace(f"<@{bot.user.id}>", "").strip()
            if clean_content:
                async with channel.typing():
                    try:
                        reply_text = await asyncio.to_thread(
                            gemini_analyzer.ask_general,
                            question=clean_content,
                        )
                        await message.reply(reply_text)
                    except Exception as e:
                        await message.reply(f"⚠️ 답변 생성 중 오류가 발생했습니다: {e}")
            return

        await bot.process_commands(message)

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
                    f"⚠️ Discord API HTTP {e.status} 감지. "
                    f"{retry_delay}초 대기 후 재시도합니다...",
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
