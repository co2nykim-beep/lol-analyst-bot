import os
import threading
import asyncio
import traceback
from flask import Flask
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from riot_client import RiotClient
from gemini_analyzer import GeminiAnalyzer

# --- Render 24시간 가동용 Flask 서버 설정 ---
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = threading.Thread(target=run)
    t.daemon = True
    t.start()

keep_alive()
# ----------------------------------------

load_dotenv()
DISCORD_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
GEMINI_KEY = os.getenv('GEMINI_API_KEY')
RIOT_KEY = os.getenv('RIOT_API_KEY')
MODEL_NAME = os.getenv('GEMINI_MODEL', 'gemini-3.6-flash')

riot_client = RiotClient(api_key=RIOT_KEY)
gemini_analyzer = GeminiAnalyzer(api_key=GEMINI_KEY, model_name=MODEL_NAME)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

lol_group = app_commands.Group(name="롤", description="LoL AI 코치 분석 명령어 모음")

async def send_embed_response(interaction: discord.Interaction, embed: discord.Embed, view=None):
    """안전하게 디스코드 Embed 메시지를 전송하는 헬퍼 함수"""
    try:
        kwargs = {"embed": embed}
        if view is not None:
            kwargs["view"] = view
        
        if interaction.response.is_done():
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)
    except Exception as e:
        print(f"응답 전송 중 오류 발생: {e}")

@bot.event
async def on_ready():
    print(f"=== {bot.user.name} 봇 준비 완료! ===")
    try:
        bot.tree.add_command(lol_group)
        synced = await bot.tree.sync()
        print(f"등록된 슬래시 명령어 개수: {len(synced)}개")
    except Exception as e:
        print(f"명령어 동기화 실패: {e}")

# 1. /롤 전적
@lol_group.command(name="전적", description="소환사의 최근 전적 데이터를 AI가 분석합니다.")
@app_commands.describe(game_name="소환사 이름", tag_line="태그 (예: KR1)")
async def match_analysis(interaction: discord.Interaction, game_name: str, tag_line: str):
    try:
        await interaction.response.defer()
    except Exception:
        pass

    try:
        account = await riot_client.get_account_by_riot_id(game_name, tag_line)
        if not account:
            await interaction.followup.send(f"❌ `{game_name}#{tag_line}` 소환사를 찾을 수 없습니다.")
            return

        puuid = account['puuid']
        matches = await riot_client.get_recent_matches(puuid, count=5)
        
        if not matches:
            await interaction.followup.send(f"⚠️ 최근 게임 데이터를 불러올 수 없습니다.")
            return

        summary_text = f"소환사 {game_name}#{tag_line} 최근 5경기 매치 ID 목록: {', '.join(matches)}"
        
        # asyncio.to_thread로 이벤트 루프 멈춤 방지
        analysis_res = await asyncio.to_thread(
            gemini_analyzer.analyze_match_history, f"{game_name}#{tag_line}", summary_text
        )

        embed = discord.Embed(
            title=f"📊 {game_name}#{tag_line} AI 전적 분석 결과",
            color=discord.Color.blue()
        )
        embed.add_field(name="💡 AI 코치 총평", value=analysis_res, inline=False)
        
        await send_embed_response(interaction, embed)
    except Exception as e:
        print(f"전적 분석 처리 에러: {e}")
        await interaction.followup.send(f"⚠️ 처리 중 오류가 발생했습니다: {e}")

# 2. /롤 인게임
@lol_group.command(name="인게임", description="현재 진행 중인 게임의 대전 팁과 승리 플랜을 분석합니다.")
@app_commands.describe(game_name="소환사 이름", tag_line="태그 (예: KR1)")
async def ingame_analysis(interaction: discord.Interaction, game_name: str, tag_line: str):
    try:
        await interaction.response.defer()
    except Exception:
        pass

    try:
        account = await riot_client.get_account_by_riot_id(game_name, tag_line)
        if not account:
            await interaction.followup.send(f"❌ `{game_name}#{tag_line}` 소환사를 찾을 수 없습니다.")
            return

        puuid = account['puuid']
        game_data = await riot_client.get_active_game(puuid)
        if not game_data:
            await interaction.followup.send(f"🎮 `{game_name}#{tag_line}` 님은 현재 진행 중인 게임이 없습니다.")
            return

        participants = game_data.get('participants', [])
        my_participant = next((p for p in participants if p.get('puuid') == puuid), None)
        
        if not my_participant:
            await interaction.followup.send("❌ 게임 참가자 정보를 불러오는데 실패했습니다.")
            return

        my_team_id = my_participant['teamId']
        my_champ = str(my_participant['championId'])
        
        my_team = [str(p['championId']) for p in participants if p['teamId'] == my_team_id]
        enemy_team = [str(p['championId']) for p in participants if p['teamId'] != my_team_id]
        
        # asyncio.to_thread로 이벤트 루프 멈춤 방지
        analysis_res = await asyncio.to_thread(
            gemini_analyzer.analyze_ingame,
            my_champ,
            "상대 라이너",
            my_team,
            enemy_team
        )

        embed = discord.Embed(
            title=f"⚔️ {game_name}#{tag_line} 실시간 인게임 코칭",
            color=discord.Color.brand_green()
        )
        embed.add_field(name="💡 AI 코치 분석", value=analysis_res, inline=False)
        
        await send_embed_response(interaction, embed)
    except Exception as e:
        print(f"인게임 분석 처리 에러: {e}")
        await interaction.followup.send(f"⚠️ 처리 중 오류가 발생했습니다: {e}")

# 3. /롤 팁
@lol_group.command(name="팁", description="라인전 대전상대 챔피언 맞대결 팁을 조회합니다.")
@app_commands.describe(my_champ="내 챔피언 이름", vs_champ="상대 챔피언 이름")
async def champion_tip(interaction: discord.Interaction, my_champ: str, vs_champ: str):
    try:
        await interaction.response.defer()
    except Exception:
        pass

    try:
        # asyncio.to_thread로 이벤트 루프 멈춤 방지
        tip_msg = await asyncio.to_thread(
            gemini_analyzer.get_champion_tip, my_champ, vs_champ
        )

        embed = discord.Embed(
            title=f"🥊 {my_champ} vs {vs_champ} 라인전 맞대결 팁",
            color=discord.Color.gold()
        )
        embed.add_field(name="💡 AI 코치 조언", value=tip_msg, inline=False)
        
        await send_embed_response(interaction, embed)
    except Exception as e:
        print(f"롤팁 처리 에러: {e}")
        await interaction.followup.send(f"⚠️ 팁을 생성하는 동안 오류가 발생했습니다: {e}")

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
