# Job finder reference

Facts about `finder.py`, its files, and its deployment, as of Nov 13, 2026. For tasks, read [USER_GUIDE.md](USER_GUIDE.md). For the reasons behind the design, read [ARCHITECTURE.md](ARCHITECTURE.md).

## Commands

| Command | Effect |
| --- | --- |
| `python3 finder.py` | Runs forever. Checks boards every `TICK_SECONDS`. |
| `python3 finder.py --once` | Checks every board in the watchlist once, sends a report if one is due, and exits. |
| `python3 finder.py --once --limit 50` | Same, for the first 50 boards only. |
| `python3 finder.py --once --emit-existing` | Same, but also reports matching jobs on boards it has never checked. Use it for testing. |
| `python3 finder.py selftest` | Runs the built-in assertions and prints `selftest passed`. Makes no network requests. |

## Settings in `finder.py`

| Name | Value | Meaning |
| --- | --- | --- |
| `TICK_SECONDS` | `300` | Time from the start of one check to the start of the next. |
| `PRIORITY_COUNT` | `300` | Number of boards, from the top of the watchlist, checked at every tick. |
| `REST_CYCLE_TICKS` | `24` | The finder checks every other board once per this many ticks, which is 2 hours. The slice comes from the clock. |
| `WORKERS` | `16` | Boards fetched at the same time. |
| `MAX_AGE_DAYS` | `1` | Oldest posting age the finder reports. For Workday, `1` allows "Posted Today" and "Posted Yesterday". |
| `REPOST_WINDOW_DAYS` | `90` | The finder skips a role whose company and title it reported within this many days. |
| `REPORT_EVERY_SECONDS` | `3600` | Shortest time between two reports. The finder stores the last report time in the `meta` table, so restarts keep it. |
| `PROBE_PER_DAY` | `500` | Most board-name guesses the daily rebuild re-checks. It re-checks the oldest first. |
| `WORKDAY_UNSORTED` | `{"xboxgaming.wd1.myworkdayjobs.com"}` | Workday hosts whose lists are not newest first. The finder reads all of their pages. |

## Environment variables

Each feature stays off until you set its variables. The server's values live in `/etc/systemd/system/job-finder.service`.

| Variable | Feature | Value on the server |
| --- | --- | --- |
| `SNS_TOPIC_ARN` | Reports | `arn:aws:sns:us-east-1:<account>:job-finder` |
| `REPLY_QUEUE_URL` | Replies | `https://sqs.us-east-1.amazonaws.com/<account>/job-replies` |
| `MY_PHONE` | Replies | Your phone in E.164 form, such as `+1XXXXXXXXXX`. The finder ignores replies from other numbers. |
| `GOOGLE_SHEET_ID` | Tracker sheet | Not set |
| `GOOGLE_CREDENTIALS` | Tracker sheet | Not set. Defaults to `google_service_account.json` next to `finder.py`. |

Reports and replies need `boto3`. The sheet needs `gspread`. The server has both in `/home/ubuntu/venv`.

## Files

| File | Written by | Contents |
| --- | --- | --- |
| `finder.py` | You | The whole program. |
| `extra_companies.txt` | You | Your list of companies, checked every tick. |
| `watchlist.json` | `build_watchlist` | Every board to check, your list first, then by Simplify listing count. Rebuilt when older than one day. |
| `all_companies.txt` | `build_watchlist` | A readable list of every company in the watchlist. |
| `probed.json` | `probe_all` | Board-name guesses for each Y Combinator company and each name-only line in your list. An entry goes stale after 7 days, and each rebuild re-checks at most `PROBE_PER_DAY` stale entries. |
| `state.db` | `tick`, `pick`, `send_report`, `sync_sheet` | SQLite database. See [Database tables](#database-tables). |
| `SETUP.md` | You | Steps to build the server and AWS resources from scratch. |
| `private/` | You | Your own copies of the guides with real values, and your toll-free registration answers. Git ignores it. |

## `extra_companies.txt` line formats

A `#` starts a comment. The finder skips blank lines.

| Form | Example | Result |
| --- | --- | --- |
| Name and board link | `Epic Games  https://job-boards.greenhouse.io/epicgames` | Adds that board under that name. |
| Board link alone | `https://jobs.ashbyhq.com/zingage` | Adds that board under its board ID. |
| Name, board type, IDs | `Netflix (incl. Games) \| eightfold \| explore.jobs.netflix.net \| netflix.com` | Adds a board read by a custom fetcher. The board type must be a key of `FETCHERS`. |
| Name alone | `Natoma` | Tries the name on Ashby, Greenhouse, Lever, and Breezy at the next rebuild. |

## Supported job boards

| Board type | Link pattern | Request | Posted date |
| --- | --- | --- | --- |
| `greenhouse` | `greenhouse.io/<board>` | 1 | `first_published` |
| `lever` | `jobs.lever.co/<board>` | 1 | none. `createdAt` is the draft time. |
| `ashby` | `jobs.ashbyhq.com/<board>` | 1 | `publishedAt` |
| `smartrecruiters` | `jobs.smartrecruiters.com/<board>` | 1, newest 100 | `releasedDate` |
| `workday` | `<tenant>.wd<N>.myworkdayjobs.com/<site>` | 1 to 5, 20 jobs each | `postedOn` text |
| `breezy` | `<board>.breezy.hr` | 1 | `published_date` |
| `rippling` | `ats.rippling.com/<board>` | 1 | none |
| `workable` | `apply.workable.com/<account>` | 1 | `published_on`, date only |
| `jobvite` | `jobs.jobvite.com/<board>` | 1 | none |
| `jobscore` | `careers.jobscore.com/jobs/<board>` | 1 | `opened_date` |
| `gem` | `jobs.gem.com/<board>` | 1 | `first_published_at` |
| `avature` | custom line only (EA) | 1 to 50, 20 jobs each | none |
| `valve` | custom line only | 1 | none |
| `pcsx` | custom line only (Microsoft) | 1 to 10, 3 seconds apart | `postedTs` |
| `eightfold` | custom line only (Netflix) | 1 to 60 | `t_create` |
| `paycom` | custom line only (Sega) | 2 or more | `postedOn`, often empty |
| `phenom` | custom line only (ZeniMax) | 1 per 100 jobs | `postedDate` |

## Database tables

`state.db` has four tables.

| Table | Columns | Meaning |
| --- | --- | --- |
| `seen` | `key`, `first_seen` | Every job the finder has seen. `key` is `<board key>#<job id>`. |
| `seeded` | `board` | Every board that has returned at least one job. |
| `meta` | `name`, `value` | Values that survive restarts. `last_report` is the time of the last report. |
| `roles` | `id`, `key`, `found_at`, `company`, `title`, `location`, `pay`, `ats`, `url`, `published`, `priority`, `listings`, `status`, `picked_at`, `reported`, `sheet_state` | Every reported role. `id` is the number in texts and emails. `status` is `new` or `picked`. `reported` is `1` after a report includes the role. `sheet_state` is `add`, `update`, or `ok`. |

## Messages

| Message | Format |
| --- | --- |
| Report text | `JobFinder: <n> new roles #<first>-<last> (<a> full-time, <b> intern). Notable: <up to 3 companies>. Reply with #s for resumes.` At most 160 characters. |
| Report email | The subject is `JobFinder: <n> new roles #<first>-<last>`. The body holds the report text, then one block per role with number, company, title, location, pay, and link. |
| Reply | Any text from `MY_PHONE`. The finder reads every number in it as a role number. |
| Confirmation | `JobFinder: Picked for resumes: #<id> <company>, ...` and `. Not found: #<id>` for numbers with no role. |
| Reply with no numbers | `JobFinder: Reply with role numbers, like 214 219. Reply STOP to opt out.` |

## Tracker sheet columns

The finder writes columns A to J and never writes K to M.

`ID`, `Found`, `Company`, `Title`, `Location`, `Pay`, `Job board`, `Link`, `Status`, `Picked at`, `Resumes`, `Applied`, `Notes`

## AWS and server resources

All resources are in `us-east-1`.

| Resource | Name | Notes |
| --- | --- | --- |
| Lightsail instance | `job-finder-server` | `micro_3_0` plan, $7 a month, Ubuntu 24.04, a public IPv4 address. Inbound port 22 only. |
| Lightsail key pair | `job-finder` | Private key at `~/.ssh/job-finder-lightsail` on your Mac. |
| systemd service | `job-finder` | Runs `/home/ubuntu/venv/bin/python -u finder.py` in `/home/ubuntu/job_finder`. Restarts 30 seconds after a crash. |
| SNS topic | `job-finder` | Reports. Email subscription to your address, confirmed. |
| SNS topic | `job-replies` | Receives inbound texts. |
| SQS queue | `job-replies` | Subscribed to `job-replies` with raw message delivery. Keeps messages for 14 days. |
| IAM user | `job-finder` | `sns:Publish` on `job-finder`, and `sqs:ReceiveMessage` and `sqs:DeleteMessage` on `job-replies`. Access key in `~/.aws/credentials` on the server. |
| IAM role | `job-finder-inbound-texts` | Lets `sms-voice.amazonaws.com` publish to `job-replies`. |
| Toll-free number | `+1877XXXXXXX` | $2 a month. Two-way texting to `job-replies`. Sends nothing until its registration is approved. |
| Verified phone | your `MY_PHONE` | Pending. AWS sends the code after the number is approved. |
| Budget | `job-finder-monthly` | $15 a month. Emails at 80% of actual spend and 100% of forecast spend. |
| SMS spend limit | account setting | $1 a month, the default for new accounts. |
