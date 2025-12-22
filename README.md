# DTEK-bot

## Telegram bot: monitors [DTEK KEM site](https://www.dtek-kem.com.ua/ua/shutdowns) updates for user-selected address

- /set  -> asks Street -> asks House
- /check -> check now
- /status -> show saved address + last updateTimestamp
- /stop -> forget address and stop monitoring
- Buttons for quick actions
- Periodic polling via PTB JobQueue

## Install:
```
  pip install "python-telegram-bot[job_queue]==20.*" playwright
  playwright install
```

## Run:
```
cd /path/to/repo
BOT_TOKEN="PASTE_NEW_TOKEN_HERE" sudo ./scripts/service.sh install
```

## Logs:
```
./scripts/service.sh logs
```

## Status
```
./scripts/service.sh status
```

## Uninstall
```
sudo ./scripts/service.sh uninstall
```
