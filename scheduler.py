import schedule
import time
import logging
from main import run_agent

logging.basicConfig(level=logging.INFO)

# Every Monday at 07:30 IST
schedule.every().monday.at("07:30").do(run_agent)

# Optional: Also run on Wednesday for mid-week check
# schedule.every().wednesday.at("08:00").do(run_agent)

print("Scheduler active — runs every Monday 07:30 IST")
print("Press Ctrl+C to stop\n")

while True:
    schedule.run_pending()
    time.sleep(30)
