# lib/scheduler.py

import subprocess
from datetime import datetime, timedelta, timezone
import threading
from filelock import FileLock

class Scheduler:
    def __init__(self, name, target_script, start_utc, interval, countdown_vars, update_countdown_status):
        self.name = name
        self.target_script = target_script
        self.activity = 0
        self.start_utc = start_utc
        self.interval = interval
        self.scheduler_enabled = False
        self.scheduler_timer = None
        self.countdown_updater = None
        self.countdown_remaining = 0  # In seconden
        self.countdown_vars = countdown_vars
        self.update_countdown = update_countdown_status
    
    def update_parameters(self, start_utc, interval):
        self.start_utc = start_utc
        self.interval = interval

    def calculate_next_run(self, now):
        if now < self.start_utc:
            return self.start_utc
        elapsed = now - self.start_utc
        intervals_passed = int(elapsed.total_seconds() // self.interval.total_seconds())
        return self.start_utc + (intervals_passed + 1) * self.interval

    def execute_script(self):
        if not self.scheduler_enabled:
            print(f"[{self.name}] ⚠️ Scheduler is disabled. Skipping execution.")
            return

        try:
            self.activity = 1
            print(f"[{self.name}] ⏳ Executing {self.target_script} at {datetime.now(timezone.utc).isoformat()}")
            subprocess.run(["python", self.target_script])
            self.activity = 0
        except Exception as e:
            print(f"[{self.name}] ⚠️ Error while running {self.target_script}: {e}")

        # Plan next run
        self.schedule_next_run()

    def schedule_next_run(self):
        now = datetime.now(timezone.utc)
        next_run = self.calculate_next_run(now)
        wait_time = (next_run - now).total_seconds()

        print(f"[{self.name}] [{now.isoformat()}] Next execution at {next_run.isoformat()} UTC (in {int(wait_time)} seconds)")

        # Stel de countdown-timer in
        self.countdown_remaining = int(wait_time)
        self.start_countdown_updates()

        # Plan daadwerkelijke scriptuitvoering
        self.scheduler_timer = threading.Timer(wait_time, self.execute_script)
        self.scheduler_timer.start()

    def start(self):
        if not self.scheduler_enabled:
            print(f"[{self.name}] ✅ Scheduler started.")
            self.scheduler_enabled = True
            self.schedule_next_run()

    def stop(self):
        if self.scheduler_enabled:
            self.scheduler_enabled = False
            if self.scheduler_timer is not None:
                self.scheduler_timer.cancel()
            if self.countdown_updater is not None:
                self.countdown_updater.cancel()
            
            # Print status
            print(f"[{self.name}] ⛔ Scheduler stopped.")
            
            # Update counter
            self.countdown_remaining = 0

    def is_running(self):
        return self.scheduler_enabled
    
    def start_countdown_updates(self):
        if self.countdown_updater:
            self.countdown_updater.cancel()

        if self.countdown_remaining <= 0:
            return
  
        self.countdown_remaining -= 1

        self.countdown_updater = threading.Timer(1.0, self.start_countdown_updates)
        self.countdown_updater.start()