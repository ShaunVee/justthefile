<p align="center">
  <img src="assets/logo.png" alt="justthefile" width="128" height="128">
</p>

<h1 align="center">justthefile</h1>

<p align="center">
  <strong>Paste an X, Reddit or Facebook link into a web page, or send one to a Telegram bot.<br>Get the video back as a real mp4.</strong>
</p>

<p align="center">
  No mirror page. No interstitial. No ads. Just the file.
</p>

<p align="center">
  No API keys. No developer account. No cookies. No login.
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white">
  <img alt="python-telegram-bot" src="https://img.shields.io/badge/python--telegram--bot-21.9-26A5E4?style=flat-square&logo=telegram&logoColor=white">
  <img alt="Docker" src="https://img.shields.io/badge/Docker-compose-2496ED?style=flat-square&logo=docker&logoColor=white">
  <img alt="FFmpeg" src="https://img.shields.io/badge/FFmpeg-transcoding-007808?style=flat-square&logo=ffmpeg&logoColor=white">
  <img alt="SQLite" src="https://img.shields.io/badge/SQLite-file__id_cache-003B57?style=flat-square&logo=sqlite&logoColor=white">
  <img alt="asyncio" src="https://img.shields.io/badge/asyncio-non--blocking-blue?style=flat-square">
</p>

<p align="center">
  <a href="https://justthefile.com"><strong>➜ justthefile.com</strong></a>
  &nbsp;·&nbsp;
  <a href="https://t.me/xwitter_downloader_bot"><strong>@xwitter_downloader_bot</strong></a>
  &nbsp;·&nbsp;
  <a href="https://t.me/rddt_downloader_bot"><strong>@rddt_downloader_bot</strong></a>
  &nbsp;·&nbsp;
  <a href="https://t.me/fbook_downloader_bot"><strong>@fbook_downloader_bot</strong></a>
</p>

---

## Use it

**On the web**: open **[justthefile.com](https://justthefile.com)**, paste a link
to an X, Reddit or Facebook post, pick a quality. No 50 MB ceiling, and you get
the original.

**On Telegram**: **[@xwitter_downloader_bot](https://t.me/xwitter_downloader_bot)**
for X, **[@rddt_downloader_bot](https://t.me/rddt_downloader_bot)** for Reddit,
**[@fbook_downloader_bot](https://t.me/fbook_downloader_bot)** for Facebook.
Hit **Start**, paste a link, get the video back as an mp4.

Either way: nothing to install, no account to make.

---

## What it does

| | |
|---|---|
| 🎬 | **Real mp4 files**, playable inline and saveable, not a link to a mirror site |
| 🖼️ | **Multi-video posts**, GIFs as looping animations, photos at original resolution |
| 🔗 | **Short links** resolved automatically: `t.co`, `redd.it`, and Reddit's `/s/` share links |
| 🔊 | **Reddit video arrives with sound**, which most tools drop |
| 📏 | **Smart size-fitting**: picks the best quality that fits Telegram's upload cap |
| ⚡ | **Instant repeats**: previously sent videos return from cache with zero re-download |
| 🎞️ | **Oversized videos still arrive**: compressed to fit, or as a direct link if compressing would ruin them |
| 🕵️ | **Nothing to sign up for**: no account, no login, no cookies. The cache records posts, never who asked for them |

---

## Supported platforms

| Platform | Links accepted |
|---|---|
| **X (Twitter)** | `x.com`, `twitter.com`, `t.co` |
| **Reddit** | `reddit.com`, `redd.it`, `/s/` share links |
| **Facebook** | `facebook.com`, `fb.watch`, `fb.com`, `/share/` links |

---

## Run your own

```bash
cp .env.example .env    # bot tokens from @BotFather
docker compose up -d    # both bots + the web front end
```

Everything else is a comment in `.env.example`. The web front end binds to
`127.0.0.1:8080`, so put a TLS terminator in front of it.

Reddit blocks a lot of hosting providers by IP address. If yours is one,
[relay/README.md](relay/README.md) is the way round it.

---

## Notes

This relies on undocumented endpoints that can change without warning, which is
why every platform here resolves through two independent providers rather than
one.

Downloaded video remains subject to whatever rights the original poster holds.
Intended for personal archiving of content you're entitled to keep.
