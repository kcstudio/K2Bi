---
name: invest-scheduler
description: Create, list, pause, resume, and delete persistent scheduled tasks for K2Bi. Use when Keith says /schedule, "schedule", "remind me", "set up a recurring", "every week do X", "run this daily", "automate", or wants any task to run on a timer. Handles both recurring cron schedules and one-time reminders. Backs onto K2B's shared k2b-remote scheduler service.
---

# K2Bi Scheduler (shared with K2B)

Manage persistent scheduled tasks via k2b-remote's SQLite database on the Mac Mini. Tasks run automatically -- k2b-remote polls every 60 seconds, executes due tasks through Claude, and sends results to Keith via Telegram.

Two task types:
- **recurring** -- runs on a cron schedule (daily, weekly, etc.)
- **one-time** -- fires once at a specific datetime, then auto-deletes

## Step 0: Check Which Machine You're On

**ALWAYS run this first before any schedule-cli command:**

```bash
hostname && whoami
```

| hostname contains | user | You are on | How to run commands |
|---|---|---|---|
| `Mac-mini` | `fastshower` | **Mac Mini** | Run locally (no SSH) |
| anything else | `keithmbpm2` | **MacBook** | SSH to Mac Mini |

**If you're on the Mac Mini (via Telegram), you are ALREADY on the right machine. Do NOT SSH.**

## Running Commands

```bash
# STEP 1: Determine the command prefix based on hostname check above

# If Mac Mini (local):
SCHED="cd ~/Projects/K2B/k2b-remote && node dist/schedule-cli.js"

# If MacBook (remote):
SCHED='ssh macmini "cd ~/Projects/K2B/k2b-remote && node dist/schedule-cli.js'
# (close the double-quote after the full command)
```

## Commands

- `/schedule <frequency> "<prompt>"` -- Create a recurring task
- `/schedule once "<datetime>" "<prompt>"` -- Create a one-time reminder
- `/schedule list` -- Show all scheduled tasks
- `/schedule pause <task-id>` -- Pause a task
- `/schedule resume <task-id>` -- Resume a paused task
- `/schedule delete <task-id>` -- Delete a task

## Creating a Recurring Task

### Step 1: Parse the request

Extract:
- **prompt**: The instruction K2B will execute
- **cron**: Parse frequency into a cron expression (see table below)
- **chatId**: `8394008217` (Keith's Telegram chat ID)

### Frequency Parsing

All times in Keith's LOCAL timezone (HKT, UTC+8).

| Input | Cron Expression | Notes |
|-------|----------------|-------|
| `daily` or `daily 9am` | `0 9 * * *` | Default 9am if no time given |
| `weekly` or `weekly monday` | `0 9 * * 1` | Default Monday 9am |
| `weekly wednesday 2pm` | `0 14 * * 3` | Specific day and time |
| `weekdays 8:30am` | `30 8 * * 1-5` | Mon-Fri |
| `monthly` | `0 9 1 * *` | 1st of month, 9am |
| `every 2h` | `0 */2 * * *` | Every 2 hours |
| `friday 4pm` | `0 16 * * 5` | Every Friday |

### Step 2: Create the task

```bash
# Mac Mini (local):
cd ~/Projects/K2B/k2b-remote && node dist/schedule-cli.js create "<prompt>" "<cron>" 8394008217

# MacBook (SSH):
ssh macmini "cd ~/Projects/K2B/k2b-remote && node dist/schedule-cli.js create \"<prompt>\" \"<cron>\" 8394008217"
```

### Step 3: Confirm to Keith

Show: Task ID, schedule (human-readable + cron), next run time, the prompt.

## Creating a One-Time Reminder

Use for "remind me", "on [date] do X", or any single-fire task.

### Step 1: Parse the request

Extract:
- **prompt**: What to remind Keith about
- **datetime**: Convert to `YYYY-MM-DD HH:MM` format in HKT
- **chatId**: `8394008217`

### Step 2: Create the reminder

```bash
# Mac Mini (local):
cd ~/Projects/K2B/k2b-remote && node dist/schedule-cli.js create-once "<prompt>" "<YYYY-MM-DD HH:MM>" 8394008217

# MacBook (SSH):
ssh macmini "cd ~/Projects/K2B/k2b-remote && node dist/schedule-cli.js create-once \"<prompt>\" \"<YYYY-MM-DD HH:MM>\" 8394008217"
```

### Step 3: Confirm to Keith

Show: Task ID, fire time, the prompt. Note it auto-deletes after firing.

## Listing Tasks

```bash
# Mac Mini (local):
cd ~/Projects/K2B/k2b-remote && node dist/schedule-cli.js list

# MacBook (SSH):
ssh macmini "cd ~/Projects/K2B/k2b-remote && node dist/schedule-cli.js list"
```

Format output as a table:

```
| ID | Type | Schedule | Status | Next Run | Prompt |
|----|------|----------|--------|----------|--------|
```

## Pausing/Resuming/Deleting

```bash
# Mac Mini (local):
cd ~/Projects/K2B/k2b-remote && node dist/schedule-cli.js pause <id>
cd ~/Projects/K2B/k2b-remote && node dist/schedule-cli.js resume <id>
cd ~/Projects/K2B/k2b-remote && node dist/schedule-cli.js delete <id>

# MacBook (SSH):
ssh macmini "cd ~/Projects/K2B/k2b-remote && node dist/schedule-cli.js pause <id>"
ssh macmini "cd ~/Projects/K2B/k2b-remote && node dist/schedule-cli.js resume <id>"
ssh macmini "cd ~/Projects/K2B/k2b-remote && node dist/schedule-cli.js delete <id>"
```

## Usage Logging

After creating, modifying, or listing tasks, append to the usage log:

```bash
echo -e "$(date +%Y-%m-%d)\tinvest-scheduler\t$(echo $RANDOM | md5 | head -c 8)\tcreated/listed/paused/deleted task" >> ~/Projects/K2Bi-Vault/wiki/context/skill-usage-log.tsv
```

## Examples

### One-time reminder
```
/schedule once "Apr 2 6pm" "Bring driving license for Shanghai car rental pickup"
```

### Weekly research
```
/schedule weekly wednesday "Run /research external 'AI recruiting tools' and save to review/"
```

### Daily check
```
/schedule daily 5pm "Check review/ for items older than 3 days and list them"
```

## Notes

- No em dashes, no AI cliches, no sycophancy
- Tasks run on the Mac Mini, not the MacBook -- MacBook can be closed
- Results are delivered to Keith via Telegram automatically
- One-time reminders auto-delete from the database after firing
- The Mac Mini must have Clash Verge VPN running for Telegram connectivity
- When Keith asks "what reminders do I have" or similar, ALWAYS run `schedule-cli.js list` -- do NOT confuse this with inbox processing
