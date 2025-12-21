# DTEK-bot

## Telegram bot: monitors [DTEK KEM site](https://www.dtek-kem.com.ua/ua/shutdowns) updates for user-selected address

- /set  -> asks Street -> asks House
- /check -> check now
- /status -> show saved address + last updateTimestamp
- /stop -> forget address and stop monitoring
- Buttons for quick actions
- Periodic polling via PTB JobQueue

Install:
```
  pip install "python-telegram-bot[job_queue]==20.*" playwright
  playwright install
```

Run:
```
  export BOT_TOKEN="123:ABC"
  export POLL_EVERY_SEC=300
  python3 bot.py
```