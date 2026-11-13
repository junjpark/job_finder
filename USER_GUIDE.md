# Job finder user guide

These guides cover the everyday tasks for the job finder that runs on the `job-finder-server` Lightsail server. For how it works, read [ARCHITECTURE.md](ARCHITECTURE.md). For settings, files, and commands, read [REFERENCE.md](REFERENCE.md).

Placeholders such as `<server-ip>` stand for values from your own deployment. Keep a copy with your real values in `private/`, which git ignores.

Every command that starts with `ssh` runs from your Mac. The others run on the server after you connect.

## Connect to the server

Run this from your Mac:

```bash
ssh -i ~/.ssh/job-finder-lightsail ubuntu@<server-ip>
```

The key `~/.ssh/job-finder-lightsail` exists only on your Mac. If you lose it, create a new key pair in the Lightsail console and add it to the server.

## Check that the finder is running

1. Ask systemd for the service state:

	```bash
	ssh -i ~/.ssh/job-finder-lightsail ubuntu@<server-ip> 'systemctl is-active job-finder'
	```

	The answer is `active`.

2. Read the last few checks:

	```bash
	ssh -i ~/.ssh/job-finder-lightsail ubuntu@<server-ip> 'journalctl -u job-finder -o cat | grep checked | tail -3'
	```

	You see one line about every 5 minutes, such as `04:28:54 checked 501 boards, 0 new matches, 7 errors`. A handful of errors per check is normal. Those are boards that closed or block scripts.

If the newest `checked` line is more than 10 minutes old, restart the finder. The steps are in [Restart or stop the finder](#restart-or-stop-the-finder).

## Read a report

The finder sends a report at most once an hour, and only when it found a new role. A role counts as new when its job board says it went up in the last 24 hours.

- The email lists every new role with its number, company, title, location, pay, and link. Roles from companies in `extra_companies.txt` come first.
- The text is a one-line summary, such as `JobFinder: 3 new roles #214-216 (2 full-time, 1 intern). Notable: Riot Games, Epic Games, Beli. Reply with #s for resumes.`

Texts start only after AWS approves the toll-free number registration. Until then, you get email only.

## Pick roles for resumes

1. Find the numbers of the roles you want in the email or the text.
2. Reply to the text from the phone number in `MY_PHONE` with those numbers, such as `214 219`.

Within about 20 seconds, you get a confirmation such as `JobFinder: Picked for resumes: #214 Riot Games, #219 Beli.` The finder marks those roles `picked` in `state.db`. The finder ignores replies from any other phone number.

To see your picks, run this on the server:

```bash
cd ~/job_finder && python3 -c "import sqlite3; [print(r) for r in sqlite3.connect('state.db').execute(\"SELECT id, company, title, url FROM roles WHERE status='picked'\")]"
```

## Add a company

1. Open `extra_companies.txt` on your Mac.
2. Add one line for the company. Use the first form that works:
	- If you know the careers link, write the name and the link: `Epic Games  https://job-boards.greenhouse.io/epicgames`. This works for Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Breezy, Rippling, Workable, Jobvite, JobScore, and Gem boards.
	- If you only know the name, write the name: `Natoma`. The finder tries that name on Ashby, Greenhouse, Lever, and Breezy at the next daily refresh.
	- If the company runs its own careers site, ask Claude to find its feed. Those lines look like `Valve | valve | https://www.valvesoftware.com/en/`.
3. Copy the file to the server, and delete `watchlist.json` there so the finder rebuilds the list now instead of tomorrow:

	```bash
	scp -i ~/.ssh/job-finder-lightsail extra_companies.txt ubuntu@<server-ip>:job_finder/
	ssh -i ~/.ssh/job-finder-lightsail ubuntu@<server-ip> 'rm ~/job_finder/watchlist.json && sudo systemctl restart job-finder'
	```

The finder checks every company in `extra_companies.txt` every 5 minutes. Its first check of a new board records the jobs already open and reports only the ones the board dates within the last 24 hours. After that, you hear about each new posting.

To see every company the finder checks, open `all_companies.txt`. The finder rewrites it at each daily refresh.

## Change how recent a role must be

1. In `finder.py`, change `MAX_AGE_DAYS`. The value `1` means the last 24 hours. For Workday boards, `1` means "Posted Today" or "Posted Yesterday".
2. Deploy the change. The steps are in [Deploy a code change](#deploy-a-code-change).

To change how often reports go out, change `REPORT_EVERY_SECONDS` the same way. To change how long a repeat of the same company and title stays hidden, change `REPOST_WINDOW_DAYS`.

## Deploy a code change

1. Run the built-in checks on your Mac:

	```bash
	python3 finder.py selftest
	```

	The last line is `selftest passed`. If an assertion fails, fix it before you deploy.

2. Copy the file and restart the service:

	```bash
	scp -i ~/.ssh/job-finder-lightsail finder.py ubuntu@<server-ip>:job_finder/
	ssh -i ~/.ssh/job-finder-lightsail ubuntu@<server-ip> 'cd job_finder && ~/venv/bin/python finder.py selftest && sudo systemctl restart job-finder'
	```

3. After a minute, [check that the finder is running](#check-that-the-finder-is-running).

A restart keeps `state.db`, so the finder never reports a role twice and never loses a pick.

## Restart or stop the finder

- To restart: `sudo systemctl restart job-finder`
- To stop until the next reboot: `sudo systemctl stop job-finder`
- To stop for good: `sudo systemctl disable --now job-finder`

systemd also restarts the finder 30 seconds after a crash and starts it after a reboot.

## Fix common problems

### You get no texts

The toll-free number sends nothing until AWS approves its registration. Check the status in the AWS console under **AWS End User Messaging SMS > Registrations**. Approval can take up to 15 business days. If AWS asks for updates, edit the registration and resubmit it.

After approval, new AWS accounts can text about $1 a month, which is roughly 100 texts. If texts stop near the end of a month, ask AWS Support to raise the SMS spend limit. Email reports keep arriving either way.

### You get no emails

1. Check that the finder is running.
2. Check that the email subscription is confirmed. In the AWS console, open **SNS > Topics > job-finder > Subscriptions**. The email subscription shows **Confirmed**.
3. Look for failed sends on the server:

	```bash
	journalctl -u job-finder -o cat | grep 'SNS publish failed' | tail -3
	```

If nothing fails and nothing arrives, the finder found no new roles. Quiet hours are normal, especially at night in the US.

### A company never shows up in reports

Look for that board's errors:

```bash
journalctl -u job-finder -o cat | grep 'error greenhouse:epicgames' | tail -3
```

Replace `greenhouse:epicgames` with the board key from `watchlist.json`. A 404 means the company moved to a different job board. Find its new careers link and update its line in `extra_companies.txt`. Other errors on one board don't affect the rest of the check.

### The bill looks wrong

The `job-finder-monthly` budget emails you when actual spend passes $12 or forecast spend passes $15. The expected monthly total is about $10: $7 for the server, $2 for the toll-free number, and a few cents for texts.
