# LoL Analyst Discord Bot

Riot Games API의 전적·경기 타임라인과 Gemini를 연결하여 Discord에서 개인화된 League of Legends 코칭을 제공하는 봇입니다. 이 프로젝트는 Riot Games와 제휴하거나 Riot Games의 승인을 받은 제품이 아닙니다.

## 제공 기능

| 명령어 | 설명 |
| --- | --- |
| `/롤 전적 게임이름 태그` | 최근 솔로 랭크 5경기의 승률, 평균 KDA, CS/분, 주 챔피언, 티어와 맞춤형 AI 코칭을 표시합니다. |
| `/롤 복기 게임이름 태그 [match_id]` | 최근 경기 또는 지정 Match ID의 Match-V5 타임라인을 이용해 초·중·후반 사망·처치·팀 오브젝트 시점과 확인 가능한 패턴을 복기합니다. |
| `/롤 조합 내_챔피언 우리_팀 상대_팀 [표시_이름]` | 사용자가 직접 입력한 챔피언 조합으로 사전 역할·한타·오브젝트 플랜을 안내합니다. 팀 목록은 쉼표로 구분합니다. |
| `/롤 팁 내_챔피언 상대_챔피언` | 특정 라인전 매치업의 일반적인 코칭 팁을 제공합니다. |

`/롤 복기`는 공식 Match-V5 이벤트 데이터를 분석하는 **경기 종료 후 복기**입니다. 이 기능은 초·중·후반의 확인 가능한 사건, 사망·오브젝트 인접 시점, 최종 지표만 근거로 사용하며 리플레이 영상의 장면, 적 위치·쿨다운·시야 같은 비공개 실시간 정보를 판독하거나 추정하지 않습니다.

`/롤 조합`은 외부 게임 상태를 조회하지 않습니다. 사용자가 제공한 정적 챔피언 선택 정보만으로 사전 운영 플랜을 제안합니다.

## 환경 설정

`.env.example`을 복사해 `.env`를 만들고 실제 값을 채웁니다. `.env`는 절대 Git에 커밋하지 마세요.

```env
DISCORD_BOT_TOKEN=your_discord_bot_token
DISCORD_CHANNEL_ID=your_default_channel_id
GEMINI_API_KEY=your_gemini_api_key
GEMINI_MODEL=gemini-3.6-flash
RIOT_API_KEY=your_riot_api_key
```

다음 명령으로 의존성을 설치하고 봇을 시작합니다.

```bash
sudo pip3 install -r requirements.txt
python3 bot.py
```

Discord Developer Portal에서 **Message Content Intent**를 활성화해야 AI 코칭 스레드의 일반 대화와 봇 멘션 질의응답이 작동합니다. 봇 초대에는 `bot` 및 `applications.commands` 스코프와 메시지 전송·스레드 생성·스레드 메시지 전송 권한이 필요합니다.

## Render 배포

Render 환경 변수에 위의 비밀 값을 등록하고 Procfile의 시작 명령을 다음과 같이 유지합니다.

```procfile
web: python bot.py
```

봇은 Render 헬스체크용 HTTP 서버를 `PORT` 환경 변수(기본값 `10000`)에서 함께 실행합니다.

## 테스트

```bash
python3 -m unittest discover -s tests -v
```

## 정책 및 데이터 범위

Riot ID는 `게임 이름#태그` 형식을 사용합니다. Riot API 키는 코드나 공개 저장소에 포함하지 말고 환경 변수로만 주입해야 합니다. 게임 중 기능은 플레이어에게 알려지지 않은 정보를 통해 경쟁 우위를 제공하지 않도록 제한해야 합니다.

Riot 정책은 게임 클라이언트에 없는 정보를 이용해 경쟁 우위를 주는 제품을 금지합니다. 따라서 본 봇의 기본 경로는 **경기 종료 후 Match-V5 타임라인 복기**입니다. 로컬 리플레이 영상 분석은 사용자가 영상 또는 명시적으로 내보낸 데이터만 제공하는 별도 동의형 기능으로 설계해야 하며, 원격 봇이 게임 중 상태를 수집·추론하는 방식은 구현하지 않습니다. 자세한 확장 계획은 [REPLAY_REALTIME_ROADMAP.md](REPLAY_REALTIME_ROADMAP.md)를 참고하세요.
