"""Discord 엔트리포인트: 명령 바인딩, Embed 표시, 코칭 스레드 관리."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from gemini_analyzer import CoachingReport, GeminiAnalyzer
from riot_client import RiotAPIError, RiotClient

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
LOGGER = logging.getLogger("lol_analyst_bot")

QUEUE_TYPES = {
    420: "솔로 랭크",
    440: "자유 랭크",
    450: "칼바람 나락",
    400: "일반 게임",
    490: "빠른 대전",
}
ROLE_NAMES = {
    "TOP": "탑",
    "JUNGLE": "정글",
    "MIDDLE": "미드",
    "BOTTOM": "바텀",
    "UTILITY": "서포터",
    "UNKNOWN": "미확인",
}


class _HealthHandler(BaseHTTPRequestHandler):
    """Render의 Web Service 헬스체크를 위한 최소 HTTP 응답기."""

    def do_GET(self) -> None:  # noqa: N802 - http.server 인터페이스
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"LoL Analyst Discord Bot is alive.")

    def do_HEAD(self) -> None:  # noqa: N802 - http.server 인터페이스
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


def keep_alive() -> None:
    """Render가 할당한 포트에서 별도 스레드로 헬스체크 서버를 시작한다."""
    port = int(os.environ.get("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True, name="health-check").start()
    LOGGER.info("헬스체크 서버 시작: port=%s", port)


def format_rank(entries: list[dict[str, Any]]) -> str:
    """League-V4 응답을 Embed용 간결한 티어 텍스트로 바꾼다."""
    def one(queue_type: str) -> str:
        entry = next((item for item in entries if item.get("queueType") == queue_type), None)
        if not entry:
            return "언랭크"
        return (
            f"{entry.get('tier', '')} {entry.get('rank', '')} "
            f"{entry.get('leaguePoints', 0)}LP "
            f"({entry.get('wins', 0)}승 {entry.get('losses', 0)}패)"
        )

    return f"솔로: {one('RANKED_SOLO_5x5')}\n자유: {one('RANKED_FLEX_SR')}"


def clip_text(text: str, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else f"{text[: limit - 1].rstrip()}…"


def build_performance_embed(
    *,
    summoner_name: str,
    queue_name: str,
    performance: dict[str, Any],
    rank_text: str,
    report: CoachingReport,
    champion_icon_url: str | None,
) -> discord.Embed:
    """최근 전적 분석 결과를 Discord Embed 카드로 생성한다."""
    games = performance.get("games", 0)
    avg_kda = (
        f"{performance.get('average_kills', 0):.1f}/"
        f"{performance.get('average_deaths', 0):.1f}/"
        f"{performance.get('average_assists', 0):.1f}"
    )
    role = ROLE_NAMES.get(str(performance.get("primary_role", "UNKNOWN")), "미확인")
    main_champion = performance.get("main_champion", "알 수 없음")

    embed = discord.Embed(
        title=f"{summoner_name} · {queue_name} AI 전적 코칭",
        description=clip_text(report.markdown, 3_700),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name=f"최근 {games}경기",
        value=(
            f"**{performance.get('wins', 0)}승 {performance.get('losses', 0)}패** · "
            f"승률 **{performance.get('win_rate', 0):.1f}%**\n"
            f"평균 KDA **{avg_kda}** ({performance.get('average_kda_ratio', 0):.2f}:1)"
        ),
        inline=True,
    )
    embed.add_field(
        name="주 챔피언·라인",
        value=(
            f"**{main_champion}** ({performance.get('main_champion_games', 0)}경기)\n"
            f"주 라인: **{role}** · 평균 CS: **{performance.get('average_cs_per_minute', 0):.1f}/분**"
        ),
        inline=True,
    )
    embed.add_field(name="현재 티어", value=clip_text(rank_text, 1_000), inline=False)
    embed.add_field(name="AI 한줄 피드백", value=clip_text(report.one_liner, 300), inline=False)
    embed.set_footer(text="Riot Match-V5 기반 · 경기 수가 적으면 참고용으로 해석하세요")
    if champion_icon_url:
        embed.set_thumbnail(url=champion_icon_url)
    return embed


def build_review_embed(
    *,
    summoner_name: str,
    review_data: dict[str, Any],
    report: CoachingReport,
    champion_icon_url: str | None,
) -> discord.Embed:
    """경기 종료 후 Match-V5 Timeline 복기 카드로 생성한다."""
    match = review_data.get("player_match", {})
    result = "승리" if match.get("win") else "패배"
    timeline_status = "이벤트 타임라인 반영" if review_data.get("timeline_available") else "타임라인 미제공 · 결과 지표 중심"

    embed = discord.Embed(
        title=f"{summoner_name} · {match.get('champion', 'LoL')} 경기 복기",
        description=clip_text(report.markdown, 3_700),
        color=discord.Color.green() if match.get("win") else discord.Color.red(),
    )
    embed.add_field(
        name="경기 결과",
        value=(
            f"**{result}** · {match.get('game_duration', '0:00')}\n"
            f"KDA **{match.get('kills', 0)}/{match.get('deaths', 0)}/{match.get('assists', 0)}** · "
            f"딜량 **{match.get('damage_to_champions', 0):,}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="복기 데이터",
        value=(
            f"첫 사망: **{review_data.get('first_death_time') or '없음'}**\n"
            f"사망 {review_data.get('death_count_in_timeline', 0)}회 · "
            f"개인 오브젝트 {len(review_data.get('personal_objectives', []))}회"
        ),
        inline=True,
    )
    embed.add_field(name="AI 한줄 피드백", value=clip_text(report.one_liner, 300), inline=False)
    embed.set_footer(text=f"{timeline_status} · 영상 리플레이를 직접 판독한 결과는 아닙니다")
    if champion_icon_url:
        embed.set_thumbnail(url=champion_icon_url)
    return embed


def build_composition_embed(
    display_name: str,
    my_champion: str,
    my_team: list[str],
    enemy_team: list[str],
    advice: str,
) -> discord.Embed:
    """사용자가 직접 제공한 챔피언 조합을 기반으로 한 사전 운영 조언 카드."""
    embed = discord.Embed(
        title=f"{display_name} · 챔피언 조합 코칭",
        description=clip_text(advice, 3_700),
        color=discord.Color.gold(),
    )
    embed.add_field(name="내 챔피언", value=f"**{my_champion}**", inline=True)
    embed.add_field(name="우리 팀", value=clip_text(", ".join(my_team) or "정보 없음", 1_000), inline=False)
    embed.add_field(name="상대 팀", value=clip_text(", ".join(enemy_team) or "정보 없음", 1_000), inline=False)
    embed.set_footer(text="직접 입력한 조합 기반 사전 조언 · 실시간 상태·적 위치·쿨다운은 사용하지 않습니다")
    return embed


class LolCoachBot(commands.Bot):
    """종료 시 Riot HTTP 세션까지 정리하는 Discord Bot."""

    def __init__(self, *, riot_client: RiotClient, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.riot_client = riot_client

    async def close(self) -> None:
        await self.riot_client.close_session()
        await super().close()


def create_bot(riot_client: RiotClient, gemini_analyzer: GeminiAnalyzer) -> LolCoachBot:
    """명령·이벤트를 등록한 Discord Bot 인스턴스를 생성한다."""
    intents = discord.Intents.default()
    intents.message_content = True
    bot = LolCoachBot(command_prefix="!", intents=intents, riot_client=riot_client)
    lol_group = app_commands.Group(name="롤", description="LoL AI 코치 분석 명령어")

    async def get_rank_text(puuid: str) -> str:
        try:
            return format_rank(await riot_client.get_league_entries(puuid))
        except RiotAPIError:
            return "티어 정보 조회 실패"

    async def open_coaching_thread(
        interaction: discord.Interaction,
        source_message: discord.Message,
        thread_name: str,
    ) -> int:
        """분석 카드 아래에 코칭 전용 스레드를 만들고, 실패 시 현재 채널을 사용한다."""
        channel = interaction.channel
        if isinstance(channel, discord.TextChannel):
            try:
                thread = await source_message.create_thread(
                    name=clip_text(thread_name, 100),
                    auto_archive_duration=60,
                )
                await thread.send("이 스레드에서 방금 받은 리포트에 대해 편하게 추가 질문해 주세요.")
                return thread.id
            except discord.DiscordException as error:
                LOGGER.warning("코칭 스레드 생성 실패: %s", error)
        return interaction.channel_id

    async def find_account(game_name: str, tag_line: str) -> tuple[str, str]:
        account = await riot_client.get_account_by_riot_id(game_name, tag_line)
        puuid = str(account.get("puuid", ""))
        if not puuid:
            raise RiotAPIError(404, "소환사 PUUID를 찾지 못했습니다.")
        display_name = f"{account.get('gameName', game_name)}#{account.get('tagLine', tag_line)}"
        return puuid, display_name

    @bot.event
    async def on_ready() -> None:
        LOGGER.info("Discord 연결 성공: %s", bot.user)
        try:
            synced = await bot.tree.sync()
            LOGGER.info("동기화 완료된 글로벌 슬래시 명령어: %s개", len(synced))
        except Exception:
            LOGGER.exception("슬래시 명령어 동기화 실패")

    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        LOGGER.error("슬래시 명령어 오류:\n%s", "".join(traceback.format_exception(error)))
        user_message = "처리 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(user_message, ephemeral=True)
            else:
                await interaction.response.send_message(user_message, ephemeral=True)
        except discord.NotFound:
            return

    @lol_group.command(name="전적", description="최근 5경기의 정량 지표와 AI 코칭 리포트를 조회합니다.")
    @app_commands.describe(game_name="게임 이름", tag_line="태그 (예: KR1)")
    async def match_analysis(
        interaction: discord.Interaction,
        game_name: str,
        tag_line: str,
    ) -> None:
        await interaction.response.defer(thinking=True)
        try:
            puuid, summoner_name = await find_account(game_name, tag_line)
            performance, rank_text = await asyncio.gather(
                riot_client.get_recent_performance(puuid=puuid, count=5, queue=420),
                get_rank_text(puuid),
            )
            if not performance.get("games"):
                await interaction.followup.send("최근 솔로 랭크 기록이 없습니다. 다른 모드의 기록은 아직 분석 대상이 아닙니다.")
                return

            report = await asyncio.to_thread(
                gemini_analyzer.analyze_recent_performance,
                summoner_name,
                performance,
                rank_text,
                "솔로 랭크",
            )
            try:
                icon_url = await riot_client.get_champion_icon_url(str(performance["main_champion"]))
            except RiotAPIError as error:
                LOGGER.info("챔피언 아이콘 조회 생략: %s", error)
                icon_url = None

            embed = build_performance_embed(
                summoner_name=summoner_name,
                queue_name="솔로 랭크",
                performance=performance,
                rank_text=rank_text,
                report=report,
                champion_icon_url=icon_url,
            )
            source_message = await interaction.followup.send(embed=embed, wait=True)
            session_id = await open_coaching_thread(
                interaction,
                source_message,
                f"{game_name} · 솔로 랭크 AI 코치",
            )
            await asyncio.to_thread(
                gemini_analyzer.start_coaching_session,
                session_id,
                summoner_name,
                str(performance),
                report.markdown,
            )
        except RiotAPIError as error:
            await interaction.followup.send(f"Riot API 오류: {error.message}")
        except RuntimeError as error:
            await interaction.followup.send(str(error))
        except Exception:
            LOGGER.exception("/롤 전적 처리 실패")
            await interaction.followup.send("전적 분석 중 예기치 않은 오류가 발생했습니다.")

    @lol_group.command(name="복기", description="최근 경기의 Match-V5 이벤트 타임라인으로 사후 복기합니다.")
    @app_commands.describe(
        game_name="게임 이름",
        tag_line="태그 (예: KR1)",
        match_id="선택: 특정 Match ID (비우면 가장 최근 경기)",
    )
    async def match_review(
        interaction: discord.Interaction,
        game_name: str,
        tag_line: str,
        match_id: str | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True)
        try:
            puuid, summoner_name = await find_account(game_name, tag_line)
            if not match_id:
                recent_match_ids = await riot_client.get_recent_matches(puuid, count=1)
                if not recent_match_ids:
                    await interaction.followup.send("복기할 최근 경기 기록이 없습니다.")
                    return
                match_id = recent_match_ids[0]

            review_data = await riot_client.get_match_review(match_id, puuid)
            report = await asyncio.to_thread(
                gemini_analyzer.analyze_match_review,
                summoner_name,
                review_data,
            )
            champion_name = str(review_data.get("player_match", {}).get("champion", ""))
            try:
                icon_url = await riot_client.get_champion_icon_url(champion_name)
            except RiotAPIError as error:
                LOGGER.info("챔피언 아이콘 조회 생략: %s", error)
                icon_url = None

            embed = build_review_embed(
                summoner_name=summoner_name,
                review_data=review_data,
                report=report,
                champion_icon_url=icon_url,
            )
            source_message = await interaction.followup.send(embed=embed, wait=True)
            session_id = await open_coaching_thread(
                interaction,
                source_message,
                f"{game_name} · 경기 복기 AI 코치",
            )
            await asyncio.to_thread(
                gemini_analyzer.start_coaching_session,
                session_id,
                summoner_name,
                str(review_data),
                report.markdown,
            )
        except RiotAPIError as error:
            await interaction.followup.send(f"Riot API 오류: {error.message}")
        except RuntimeError as error:
            await interaction.followup.send(str(error))
        except Exception:
            LOGGER.exception("/롤 복기 처리 실패")
            await interaction.followup.send("경기 복기 중 예기치 않은 오류가 발생했습니다.")

    @lol_group.command(name="조합", description="직접 입력한 챔피언 조합으로 사전 운영 조언을 제공합니다.")
    @app_commands.describe(
        my_champion="내 챔피언",
        my_team="우리 팀 챔피언을 쉼표로 구분 (예: 아리, 리 신, 오른, 징크스, 룰루)",
        enemy_team="상대 팀 챔피언을 쉼표로 구분",
        display_name="선택: 카드에 표시할 이름",
    )
    async def composition_coaching(
        interaction: discord.Interaction,
        my_champion: str,
        my_team: str,
        enemy_team: str,
        display_name: str | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True)
        try:
            normalize = lambda raw: [champion.strip() for champion in raw.split(",") if champion.strip()]
            my_team_list = normalize(my_team)
            enemy_team_list = normalize(enemy_team)
            if not my_team_list or not enemy_team_list:
                await interaction.followup.send("우리 팀과 상대 팀 챔피언을 각각 쉼표로 구분해 입력해 주세요.")
                return
            if my_champion.strip() and my_champion.strip() not in my_team_list:
                my_team_list.insert(0, my_champion.strip())

            advice = await asyncio.to_thread(
                gemini_analyzer.analyze_ingame,
                my_champion.strip(),
                my_team_list,
                enemy_team_list,
            )
            await interaction.followup.send(
                embed=build_composition_embed(
                    display_name or "수동 입력 조합",
                    my_champion.strip(),
                    my_team_list,
                    enemy_team_list,
                    advice,
                )
            )
        except RuntimeError as error:
            await interaction.followup.send(str(error))
        except Exception:
            LOGGER.exception("/롤 조합 처리 실패")
            await interaction.followup.send("챔피언 조합 코칭 중 예기치 않은 오류가 발생했습니다.")

    @lol_group.command(name="팁", description="특정 챔피언 간의 라인전 팁을 조회합니다.")
    @app_commands.describe(my_champ="내 챔피언", vs_champ="상대 챔피언")
    async def champion_tip(
        interaction: discord.Interaction,
        my_champ: str,
        vs_champ: str,
    ) -> None:
        await interaction.response.defer(thinking=True)
        try:
            advice = await asyncio.to_thread(gemini_analyzer.get_champion_tip, my_champ, vs_champ)
            embed = discord.Embed(
                title=f"{my_champ} vs {vs_champ} 라인전 팁",
                description=clip_text(advice, 3_700),
                color=discord.Color.teal(),
            )
            await interaction.followup.send(embed=embed)
        except RuntimeError as error:
            await interaction.followup.send(str(error))
        except Exception:
            LOGGER.exception("/롤 팁 처리 실패")
            await interaction.followup.send("챔피언 팁 생성 중 오류가 발생했습니다.")

    bot.tree.add_command(lol_group)

    @bot.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot:
            return

        channel = message.channel
        session_id = channel.id
        is_coaching_thread = isinstance(channel, discord.Thread) and "AI 코치" in channel.name
        has_active_session = gemini_analyzer.has_session(session_id)

        if is_coaching_thread or has_active_session:
            async with channel.typing():
                reply = await asyncio.to_thread(
                    gemini_analyzer.continue_coaching_session,
                    session_id,
                    message.content,
                )
                await message.reply(reply, allowed_mentions=discord.AllowedMentions.none())
            return

        if bot.user and bot.user.mentioned_in(message) and not message.mention_everyone:
            question = message.content.replace(f"<@{bot.user.id}>", "").strip()
            if question:
                async with channel.typing():
                    try:
                        reply = await asyncio.to_thread(gemini_analyzer.ask_general, question)
                        await message.reply(reply, allowed_mentions=discord.AllowedMentions.none())
                    except RuntimeError as error:
                        await message.reply(str(error), allowed_mentions=discord.AllowedMentions.none())
            return

        await bot.process_commands(message)

    return bot


async def main() -> None:
    load_dotenv()
    discord_token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not discord_token:
        LOGGER.error("DISCORD_BOT_TOKEN 환경 변수가 설정되지 않았습니다.")
        return

    riot_client = RiotClient(api_key=os.getenv("RIOT_API_KEY"))
    gemini_analyzer = GeminiAnalyzer(
        api_key=os.getenv("GEMINI_API_KEY"),
        model_name=os.getenv("GEMINI_MODEL", GeminiAnalyzer.DEFAULT_MODEL),
    )

    retry_delay = 30
    while True:
        bot = create_bot(riot_client=riot_client, gemini_analyzer=gemini_analyzer)
        try:
            async with bot:
                await bot.start(discord_token)
            return
        except discord.errors.HTTPException as error:
            if error.status in (429, 502, 504):
                LOGGER.warning("Discord API HTTP %s. %s초 후 재시도합니다.", error.status, retry_delay)
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 600)
            else:
                LOGGER.exception("Discord HTTP 오류")
                await asyncio.sleep(retry_delay)
        except Exception:
            LOGGER.exception("봇 실행 중 예외 발생")
            await asyncio.sleep(retry_delay)


if __name__ == "__main__":
    keep_alive()
    asyncio.run(main())
