import asyncio
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from riot_client import RiotClient, RiotAPIError
from gemini_analyzer import GeminiAnalyzer

# --- Render 24시간 가동용 헬스체크 서버 ---
# 수정: Flask 개발 서버(Werkzeug) 대신 파이썬 표준 라이브러리 http.server만 사용.
#       - Werkzeug 경고 완전히 사라짐
#       - flask/gunicorn 의존성 제거 (requirements.txt도 가벼워짐)
#       - 헬스체크 요청마다 로그를 찍지 않도록 log_message를 무음 처리해서
#         Render 로그가 헬스체크로 도배되는 것을 방지 (실제 에러를 찾기 쉬워짐)
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
        pass  # 헬스체크 요청 로그 억제


def keep_alive():
    port = int(os.environ.get("PORT", 10000))
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()


keep_alive()
# ----------------------------------------

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
RIOT_KEY = os.getenv("RIOT_API_KEY")
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
# 수정: 배포할 때마다 자동으로 슬래시 명령어를 동기화하지 않도록 기본값 false.
#       명령어를 추가/수정했을 때만 Render 환경변수에서 SYNC_COMMANDS=true로 바꿔
#       한 번 배포하고, 다시 false로 되돌리세요. (재배포 연타로 인한 429 방지)
SYNC_COMMANDS = os.getenv("SYNC_COMMANDS", "false").lower() == "true"

riot_client = RiotClient(api_key=RIOT_KEY)
gemini_analyzer = GeminiAnalyzer(api_key=GEMINI_KEY, model_name=MODEL_NAME)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

lol_group = app_commands.Group(name="롤", description="LoL AI 코치 분석 명령어 모음")
# add_command는 로컬 명령어 트리에 등록하는 것 뿐이라 디스코드 API 호출이 아닙니다.
# on_ready가 여러 번 불려도(재연결 등) 안전하도록 모듈 로드 시 1회만 실행합니다.
bot.tree.add_command(lol_group)


def build_embed(title: str, description: str, color: discord.Color, rank_text: str | None = None) -> discord.Embed:
    """
    공통 embed 생성 헬퍼.
    수정: 기존 코드는 embed.add_field(value=analysis_res)를 썼는데, 디스코드 embed
    필드는 값이 1024자를 넘으면 전송 자체가 400 에러로 실패합니다. Gemini가 3개 항목을
    풀어서 설명하면 1024자를 넘기기 쉬우므로, 필드 대신 description(최대 4096자)을
    사용하도록 바꿨습니다.
    """
    if len(description) > 4000:
        description = description[:4000] + "\n\n...(내용이 길어 일부 생략됨)"
    embed = discord.Embed(title=title, description=description, color=color)
    if rank_text:
        embed.add_field(name="🏆 현재 솔로랭크", value=rank_text, inline=False)
    return embed


@bot.event
async def on_ready():
    print(f"=== {bot.user.name} 봇 준비 완료! ===", flush=True)

    if SYNC_COMMANDS:
        try:
            synced = await bot.tree.sync()
            print(f"등록된 슬래시 명령어 개수: {len(synced)}개", flush=True)
        except discord.errors.HTTPException as e:
            if e.status == 429:
                print("⚠️ 디스코드 API Rate Limit(429). 몇 분 기다렸다가 다시 SYNC_COMMANDS=true로 배포하세요.", flush=True)
            else:
                print(f"명령어 동기화 HTTP 오류: {e}", flush=True)
        except Exception as e:
            print(f"명령어 동기화 실패: {e}", flush=True)
    else:
        print("SYNC_COMMANDS=false → 이번 배포에서는 슬래시 명령어 동기화를 건너뜁니다.", flush=True)


@bot.command(name="sync")
@commands.is_owner()
async def manual_sync(ctx: commands.Context):
    """봇 소유자 전용: 슬래시 명령어를 수동으로 동기화합니다. (디스코드에서 '!sync' 입력)"""
    synced = await bot.tree.sync()
    await ctx.send(f"✅ {len(synced)}개 명령어 동기화 완료")


async def _get_rank_text(puuid: str) -> str:
    try:
        entries = await riot_client.get_league_entries(puuid)
    except RiotAPIError:
        return "조회 실패"
    solo = next((e for e in entries if e.get("queueType") == "RANKED_SOLO_5x5"), None)
    if not solo:
        return "언랭"
    return f"{solo['tier']} {solo['rank']} {solo['leaguePoints']}LP ({solo['wins']}승 {solo['losses']}패)"


# 1. /롤 전적
@lol_group.command(name="전적", description="소환사의 최근 전적 데이터를 AI가 분석합니다.")
@app_commands.describe(game_name="소환사 이름", tag_line="태그 (예: KR1)")
async def match_analysis(interaction: discord.Interaction, game_name: str, tag_line: str):
    await interaction.response.defer()
    try:
        account = await riot_client.get_account_by_riot_id(game_name, tag_line)
        puuid = account["puuid"]

        rank_text = await _get_rank_text(puuid)

        match_ids = await riot_client.get_recent_matches(puuid, count=5)
        if not match_ids:
            await interaction.followup.send("⚠️ 최근 게임 기록이 없습니다.")
            return

        # 수정: 기존 코드는 여기서 매치 ID 문자열만 이어붙여서 Gemini에 넘겼습니다.
        # 매치 상세를 실제로 조회해서 챔피언/KDA/딜량/승패를 뽑아야 의미 있는 분석이 나옵니다.
        lines = []
        for match_id in match_ids:
            detail = await riot_client.get_match_detail(match_id)
            p = next(
                (pp for pp in detail["info"]["participants"] if pp["puuid"] == puuid),
                None,
            )
            if not p:
                continue
            result = "승리" if p["win"] else "패배"
            cs = p["totalMinionsKilled"] + p.get("neutralMinionsKilled", 0)
            lines.append(
                f"- {p['championName']} ({p.get('teamPosition', '?')}): "
                f"{p['kills']}/{p['deaths']}/{p['assists']} KDA, "
                f"딜량 {p['totalDamageDealtToChampions']:,}, CS {cs}, {result}"
            )

        if not lines:
            await interaction.followup.send("⚠️ 매치 상세 데이터를 가져오지 못했습니다.")
            return

        summary_text = f"현재 솔로랭크 티어: {rank_text}\n\n최근 {len(lines)}경기 기록:\n" + "\n".join(lines)

        analysis_res = await asyncio.to_thread(
            gemini_analyzer.analyze_match_history, f"{game_name}#{tag_line}", summary_text
        )

        embed = build_embed(
            title=f"📊 {game_name}#{tag_line} AI 전적 분석 결과",
            description=analysis_res,
            color=discord.Color.blue(),
            rank_text=rank_text,
        )
        await interaction.followup.send(embed=embed)

    except RiotAPIError as e:
        await interaction.followup.send(f"⚠️ {e.message}")
    except Exception as e:
        print(f"전적 분석 처리 에러: {e}", flush=True)
        await interaction.followup.send(f"⚠️ 처리 중 오류가 발생했습니다: {e}")


# 2. /롤 인게임
@lol_group.command(name="인게임", description="현재 진행 중인 게임의 팀 조합 분석과 승리 플랜을 제공합니다.")
@app_commands.describe(game_name="소환사 이름", tag_line="태그 (예: KR1)")
async def ingame_analysis(interaction: discord.Interaction, game_name: str, tag_line: str):
    await interaction.response.defer()
    try:
        account = await riot_client.get_account_by_riot_id(game_name, tag_line)
        puuid = account["puuid"]

        try:
            game_data = await riot_client.get_active_game(puuid)
        except RiotAPIError as e:
            if e.status in (401, 403):
                # 2025-10 패치 25.20 익명화 정책으로 Spectator API가 제한된 상황을 안내
                await interaction.followup.send(
                    "⚠️ 라이엇의 실시간 관전(Spectator) API 정책 변경(2025-10, 패치 25.20 익명 모드)으로 "
                    "인게임 조회가 현재 제한되어 있을 수 있습니다. 대신 `/롤 전적`으로 최근 경기 분석을 이용해주세요."
                )
                return
            raise

        if not game_data:
            await interaction.followup.send(f"🎮 `{game_name}#{tag_line}` 님은 현재 게임 중이 아닙니다.")
            return

        participants = game_data.get("participants", [])
        my_p = next((p for p in participants if p.get("puuid") == puuid), None)
        if not my_p:
            await interaction.followup.send("❌ 게임 참가자 정보를 찾을 수 없습니다.")
            return

        my_team_id = my_p["teamId"]
        # 수정: championId(숫자)를 그대로 Gemini에 넘기지 않고 Data Dragon으로 이름을 변환.
        my_champ = await riot_client.get_champion_name(my_p["championId"])
        my_team = [
            await riot_client.get_champion_name(p["championId"])
            for p in participants
            if p["teamId"] == my_team_id
        ]
        enemy_team = [
            await riot_client.get_champion_name(p["championId"])
            for p in participants
            if p["teamId"] != my_team_id
        ]

        analysis_res = await asyncio.to_thread(
            gemini_analyzer.analyze_ingame, my_champ, my_team, enemy_team
        )

        embed = build_embed(
            title=f"⚔️ {game_name}#{tag_line} 실시간 인게임 코칭",
            description=analysis_res,
            color=discord.Color.brand_green(),
        )
        await interaction.followup.send(embed=embed)

    except RiotAPIError as e:
        await interaction.followup.send(f"⚠️ {e.message}")
    except Exception as e:
        print(f"인게임 분석 처리 에러: {e}", flush=True)
        await interaction.followup.send(f"⚠️ 처리 중 오류가 발생했습니다: {e}")


# 3. /롤 팁
@lol_group.command(name="팁", description="라인전 대전상대 챔피언 맞대결 팁을 조회합니다.")
@app_commands.describe(my_champ="내 챔피언 이름", vs_champ="상대 챔피언 이름")
async def champion_tip(interaction: discord.Interaction, my_champ: str, vs_champ: str):
    await interaction.response.defer()
    try:
        tip_msg = await asyncio.to_thread(gemini_analyzer.get_champion_tip, my_champ, vs_champ)

        embed = build_embed(
            title=f"🥊 {my_champ} vs {vs_champ} 라인전 맞대결 팁",
            description=tip_msg,
            color=discord.Color.gold(),
        )
        await interaction.followup.send(embed=embed)
    except Exception as e:
        print(f"롤팁 처리 에러: {e}", flush=True)
        await interaction.followup.send(f"⚠️ 팁을 생성하는 동안 오류가 발생했습니다: {e}")


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
