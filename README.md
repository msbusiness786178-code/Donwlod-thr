# ThorDownloader

Telegram bot: m3u8 (HLS) link do -> quality (480 / 720) do -> video mp4 me mil jayegi.

## Render deploy
1. Repo GitHub pe push karo.
2. Render -> New -> **Web Service** -> repo select -> Runtime: **Docker**.
3. Environment variables: `API_ID`, `API_HASH`, `BOT_TOKEN`.
4. Deploy. (Python 3.12.1 Dockerfile me set hai, Flask port `PORT` env se leta hai.)
5. Health check path: `/health`

## Use
- `/start`
- m3u8 link bhejo, fir `480` ya `720`.
- 1.9 GB se badi file auto parts me split hoti hai.
