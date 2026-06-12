#!/usr/bin/env python3
path = "/root/.openclaw/workspace/kie-image-bot/bot.py"
with open(path) as f:
    content = f.read()
content = content.replace('***"TELEGRAM_BOT_TOKEN"]', 'os.environ["TELEGRAM_BOT_TOKEN"]')
content = content.replace('***"KIE_API_KEY"]', 'os.environ["KIE_API_KEY"]')
with open(path, "w") as f:
    f.write(content)
print("Fixed")
