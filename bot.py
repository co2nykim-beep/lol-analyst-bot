import os
import threading
from flask import Flask

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
import os
import collections
import aiohttp
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

from riot_client import RiotClient
from gemini_analyzer import GeminiAnalyzer

print("=== 1. 모듈 Import 성공 ===")

load_dotenv()
DISCORD_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
GEMINI_KEY = os.getenv('GEMINI_API_KEY')
RIOT_KEY = os.getenv('RIOT_API_KEY')
MODEL_NAME = os.getenv('GEMINI_MODEL', 'gemini-3.6-flash')

riot_client = RiotClient(api_key=RIOT_KEY)
gemini_analyzer = GeminiAnalyzer(api_key=GEMINI_KEY, model_name=MODEL_NAME)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# --- 1번 기능: 드롭다운 Select Menu UI 구현 ---
class ReportSelect(discord.ui.Select):
    def __init__(self, match_summary, ai_msg, solo_rank, account_name):
        self.match_summary = match_summary
        self.ai_msg = ai_msg
        self.solo_rank = solo_rank
        self.account_name = account_name

        options = [
            discord.SelectOption(label="🤖 AI 코칭 종합 리포트", description="Gemini AI의 핵심 피드백 확인", value="ai_report", emoji="📊"),
            discord.SelectOption(label="🏆 모스트 챔피언 TOP 3", description="최근 20경기 가장 많이 플레이한 챔피언", value="top_champs", emoji="⚔️"),
            discord.SelectOption(label="🎯 라인별 분포 및 KDA", description="포지션별 승률 및 평균 KDA 통계", value="stats", emoji="📈")
        ]
        super().__init__(placeholder="🔍 보고 싶은 분석 항목을 선택하세요...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        
        if selected == "ai_report":
            embed = discord.Embed(
                title=f"📊 {self.account_name} AI 코칭 리포트",
                description=f"**솔로랭크:** {self.solo_rank}\n\n{self.ai_msg}",
                color=discord.Color.blue()
            )
        elif selected == "top_champs":
            champs = [m['champion'] for m in self.match_summary]
            counts = collections.Counter(champs).most_common(3)
            
            txt = "### ⚔️ 최근 20경기 모스트 챔피언 TOP 3\n\n"
            for champ, cnt in counts:
                champ_matches = [m for m in self.match_summary if m['champion'] == champ]
                wins = sum(1 for m in champ_matches if m['win'])
                txt += f"• **{champ}**: {cnt}판 ({wins}승 {cnt-wins}패 / 승률 {int(wins/cnt*100)}%)\n"

            embed = discord.Embed(title=f"🏆 {self.account_name} 모스트 챔피언", description=txt, color=discord.Color.gold())

        elif selected == "stats":
            total_kills = sum(m['kills'] for m in self.match_summary)
            total_deaths = sum(m['deaths'] for m in self.match_summary)
            total_assists = sum(m['assists'] for m in self.match_summary)
            kda = round((total_kills + total_assists) / max(1, total_deaths), 2)

            txt = f"### 📈 최근 {len(self.match_summary)}경기 통계 요약\n\n"
            txt += f"• **평균 KDA:** {kda}:1 (`{total_kills}`/`{total_deaths}`/`{total_assists}`)\n"
            txt += f"• **평균 CS:** {int(sum(m['cs'] for m in self.match_summary)/len(self.match_summary))}개\n"

            embed = discord.Embed(title=f"🎯 {self.account_name} 상세 통계", description=txt, color=discord.Color.green())

        await interaction.response.edit_message(embed=embed, view=self.view)

class CoachReportView(discord.ui.View):
    def __init__(self, match_summary, ai_msg, solo_rank, account_name):
        super().__init__(timeout=180)
        self.add_item(ReportSelect(match_summary, ai_msg, solo_rank, account_name))

# --- 슬래시 명령어 그룹 ---
class LolGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="롤", description="LoL AI 코치 및 전적 분석")

lol_group = LolGroup()

async def send_embed_response(interaction: discord.Interaction, embed: discord.Embed, view: discord.ui.View = None):
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, view=view)
    else:
        await interaction.response.send_message(embed=embed, view=view)

@lol_group.command(name="전적", description="소환사 솔로랭크 티어 & 최근 20경기 AI 코칭 리포트")
@app_commands.describe(riot_id="소환사명#태그 (예: 쿠 니#쿠 니)")
async def get_stats(interaction: discord.Interaction, riot_id: str):
    await interaction.response.defer(thinking=True)

    if '#' not in riot_id:
        embed = discord.Embed(title="⚠️ 입력 형식 오류", description="`닉네임#태그` 형태로 입력해 주세요.", color=discord.Color.orange())
        await send_embed_response(interaction, embed)
        return

    game_name, tag_line = riot_id.split('#', 1)

    async with aiohttp.ClientSession() as session:
        try:
            account, status_code = await riot_client.fetch_account(session, game_name, tag_line)

            if not account:
                embed = discord.Embed(color=discord.Color.red())
                if status_code in (401, 403):
                    embed.title = "❌ Riot API 키 만료"
                    embed.description = "RIOT_API_KEY를 갱신해 주세요."
                elif status_code == 429:
                    embed.title = "⏳ 요청 한도 초과"
                    embed.description = "잠시 후 다시 시도해 주세요."
                else:
                    embed.title = "❌ 소환사 검색 실패"
                await send_embed_response(interaction, embed)
                return

            region = riot_client._get_region_route(tag_line)
            league_entries = await riot_client.fetch_league_entries(session, account['puuid'], tag_line)
            match_summary = await riot_client.get_user_recent_summary(session, account['puuid'], region=region, count=20)

            solo_rank = "Unranked"
            if league_entries:
                for entry in league_entries:
                    if entry.get("queueType") == "RANKED_SOLO_5x5":
                        solo_rank = f"{entry.get('tier')} {entry.get('rank')} ({entry.get('leaguePoints')}LP)"
                        break

            if not match_summary:
                embed = discord.Embed(title=f"🎮 {account['gameName']}#{account['tagLine']}", description="최근 전적 데이터가 없습니다.", color=discord.Color.yellow())
                await send_embed_response(interaction, embed)
                return

            ai_msg = await gemini_analyzer.analyze_match_history(account['gameName'], account['tagLine'], match_summary)

            wins = sum(1 for m in match_summary if m['win'])
            losses = len(match_summary) - wins
            account_name = f"{account['gameName']}#{account['tagLine']}"

            embed = discord.Embed(
                title=f"📊 {account_name} 코칭 리포트",
                description=f"**솔로랭크:** {solo_rank}\n**최근 20경기:** {wins}승 {losses}패 (승률 {int(wins/len(match_summary)*100)}%)\n\n{ai_msg}",
                color=discord.Color.blue() if wins >= 10 else discord.Color.red()
            )
            embed.set_footer(text="💡 아래 드롭다운 메뉴를 선택해 더 상세한 통계를 확인하세요!")

            view = CoachReportView(match_summary, ai_msg, solo_rank, account_name)
            await send_embed_response(interaction, embed, view=view)

        except Exception as e:
            embed = discord.Embed(title="⚠️ 시스템 오류", description=f"오류 발생: {e}", color=discord.Color.red())
            await send_embed_response(interaction, embed)

# --- 3번 기능: /롤 팁 챔피언 맞춤 코칭 명령어 ---
@lol_group.command(name="팁", description="특정 챔피언의 라인전 팁 및 카운터 상대법 공략")
@app_commands.describe(my_champ="내 챔피언 (예: 제드)", vs_champ="상대 챔피언 (선택사항, 예: 야스오)")
async def champion_tip(interaction: discord.Interaction, my_champ: str, vs_champ: str = ""):
    await interaction.response.defer(thinking=True)
    tip_msg = await gemini_analyzer.get_champion_tip(my_champ, vs_champ)

    title_txt = f"💡 [{my_champ}] VS [{vs_champ}] 라인전 공략" if vs_champ else f"💡 [{my_champ}] 챔피언 핵심 공략"
    embed = discord.Embed(title=title_txt, description=tip_msg, color=discord.Color.purple())
    embed.set_footer(text="Powered by Gemini 3.6 Flash")
    await send_embed_response(interaction, embed)

bot.tree.add_command(lol_group)

@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"=== {bot.user.name} 봇 준비 완료! (동기화된 명령어: {len(synced)}개) ===")
    except Exception as e:
        print(f"명령어 동기화 오류: {e}")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    if not message.content.startswith("/"):
        async with message.channel.typing():
            full_text = await gemini_analyzer.answer_general_question(message.content)
            for i in range(0, len(full_text), 1900):
                await message.channel.send(full_text[i:i+1900])
    await bot.process_commands(message)

if __name__ == '__main__':
    bot.run(DISCORD_TOKEN)
@lol_group.command(name="인게임", description="현재 진행 중인 게임의 대전 팁과 승리 플랜을 분석합니다.")
@app_commands.describe(game_name="소환사 이름", tag_line="태그 (예: KR1)")
async def ingame_analysis(interaction: discord.Interaction, game_name: str, tag_line: str):
    await interaction.response.defer()
    
    # 1. PUUID 조회
    account = await riot_client.get_account_by_riot_id(game_name, tag_line)
    if not account:
        await interaction.followup.send(f"❌ `{game_name}#{tag_line}` 소환사를 찾을 수 없습니다.")
        return

    puuid = account['puuid']
    
    # 2. 실시간 게임 데이터 조회
    game_data = await riot_client.get_active_game(puuid)
    if not game_data:
        await interaction.followup.send(f"🎮 `{game_name}#{tag_line}` 님은 현재 진행 중인 게임이 없습니다.")
        return

    # 3. 내 챔피언 및 매치업 파악
    participants = game_data.get('participants', [])
    my_participant = next((p for p in participants if p.get('puuid') == puuid), None)
    
    if not my_participant:
        await interaction.followup.send("❌ 게임 참가자 정보를 불러오는데 실패했습니다.")
        return

    my_team_id = my_participant['teamId']
    my_champ_id = my_participant['championId']
    
    my_champ = str(my_champ_id)
    my_team = [str(p['championId']) for p in participants if p['teamId'] == my_team_id]
    enemy_team = [str(p['championId']) for p in participants if p['teamId'] != my_team_id]
    
    # 4. Gemini AI 분석 요청
    analysis_res = await gemini_analyzer.analyze_ingame(
        my_champ=my_champ,
        vs_champ="상대 라이너",
        my_team=my_team,
        enemy_team=enemy_team
    )

    embed = discord.Embed(
        title=f"⚔️ {game_name}#{tag_line} 실시간 인게임 코칭",
        color=discord.Color.brand_green()
    )
    embed.add_field(name="💡 AI 코치 분석", value=analysis_res, inline=False)
    
    await interaction.followup.send(embed=embed)
