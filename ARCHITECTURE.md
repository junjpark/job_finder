# About the job finder's design

The job finder answers one question faster than the public job lists do. Which US entry-level and intern roles went up in the last day? This page explains how `finder.py` answers it, and why it is built the way it is. For tasks, read [USER_GUIDE.md](USER_GUIDE.md). For settings and file formats, read [REFERENCE.md](REFERENCE.md).

## Read the companies' own job boards

Most companies post jobs through a hosted job board such as Greenhouse, Lever, Ashby, or Workday. Each of these has a public JSON feed that lists a company's open jobs. The finder reads those feeds directly.

The alternative was to read Simplify's public new-grad and internship lists, which most students watch. On Nov 13, 2026, a comparison of 29 new Simplify listings against each job's publish time on the company's own board found that Simplify trailed by a median of 6.3 hours. Some listings trailed by 15 days. Reading the boards directly removes that delay. Simplify still matters, because its listing history is the best free record of which companies hire new grads.

## Build the watchlist from three sources

The finder keeps a list of boards to check in `watchlist.json` and rebuilds it once a day. Three sources feed it:

1. **Simplify's listing history.** Every application link in Simplify's two `listings.json` files names a job board. That gives about 4,200 boards of companies that have hired new grads or interns before.
2. **Y Combinator companies.** The finder takes every active, public, or acquired company from the yc-oss company list and guesses its board name on Ashby, Greenhouse, Lever, and Breezy. A feed that returns 404 means no board. This adds about 840 startups that never posted on Simplify. The finder caches the guesses in `probed.json`. A guess counts as stale after a week, and each daily rebuild re-checks only the 500 oldest, because checking all 5,300 names at once takes about 25 minutes and no boards get checked meanwhile. A request that fails counts as "no board" until its next re-check.
3. **`extra_companies.txt`.** Your own list. It holds companies that matter to you, such as game studios, and companies with custom careers sites that need their own code.

On Nov 13, 2026, the watchlist held 5,106 boards. `all_companies.txt` is a readable copy.

## Check the important boards every 5 minutes

Checking all 5,106 boards every 5 minutes would mean about 1.5 million requests a day. That is more load than a personal tool should put on these services. So the finder splits the list:

- The finder checks the first 300 boards every 5 minutes. Your list comes first, then the boards with the most past Simplify listings.
- It checks every other board once every 2 hours, in 24 rotating slices. The slice comes from the clock, so a restart continues the rotation instead of starting it over.

One check reads about 500 boards with 16 threads and takes about a minute. Most of that minute is waiting for servers to answer. The finder uses almost no CPU, which is why a faster language would not make it faster.

```
every 5 minutes
  load_watchlist()   rebuild once a day
  tick()             fetch ~500 boards in parallel, filter, store new roles
  send_report()      email every 30 min, text every hour, through SNS
  sync_sheet()       only if GOOGLE_SHEET_ID is set
  check_replies()    long-poll the SQS queue until the next tick
```

## Decide what counts as new

The finder reports a role only when all of these hold:

1. **The finder has never seen the job before.** The `seen` table stores every job it has seen, keyed by board and job ID. Each check loads only the keys of the boards it is about to read.
2. **On a board's first check, the job carries a recent date.** The first check records everything that is open. It reports only jobs that the board itself dates within the last 24 hours, because an undated job could have been open for months. Without this rule, adding a company would report all of its open jobs at once. An empty answer does not count as a first check, because an outage page or a changed site can look empty.
3. **The title looks entry-level and technical.** `title_ok` needs a level word such as "new grad", "intern", "2027", "Engineer I", or "SDE I", and a field word such as "software" or "data". It rejects "senior", "II", "Engineer 2", "PhD", "2026", recruiter titles, and teaching titles such as "Adjunct Faculty".
4. **The location is in the US or unclear.** The board's own country field wins when it has one, and "USA", "US", and "United States" all count. Without one, `us_status` reads each place in the location text separately and keeps the job if any place is in the US. A US city or "United States" counts first, then a foreign country or city, then a US state name or code, so "Bengaluru, IN" is India and not Indiana. "Remote" and "2 Locations" count as unclear, and the finder keeps them.
5. **The board says the job went up in the last 24 hours.** A board that gives only a date, such as Workable, counts calendar days, so a job posted on a US afternoon is not already a day old. Rules 1 and 2 are not enough alone. Workday and SmartRecruiters show only their newest 100 jobs, so when newer jobs close, older ones the finder never saw come into view. The date check stops those from looking new. Boards that give no date, such as EA, Valve, and Lever, rely on rules 1 and 2. Lever gives only the date a job was drafted, so the finder ignores it.
6. **The description names software tools or requirements.** `software_ok` reads the full job description and keeps the role only when it names at least `MIN_SOFTWARE_TERMS` (2) items such as Python, SQL, AWS, Git, "computer science", or "software development". The text comes from the job list when the service includes it (Lever, Ashby, JobScore, Gem, Netflix), from the service's single-job data feed (Greenhouse, SmartRecruiters, Workday, Workable, Rippling, Microsoft), or from the job page with its scripts removed. Page scripts and styles never count, because they mention JavaScript on every page. A titled "Engineer I" that turns out to be grounds maintenance fails here. If the description cannot be read after one retry, the role passes, so a busy server never hides a job.
7. **The company has not posted the same title in the last 90 days.** This catches reposts, which get a new job ID, and the same role listed in several cities.

Each kept role gets the next number from the `roles` table. That number is what you text back.

## Send reports and read replies through AWS

The finder publishes each report once to the SNS topic `job-finder`. SNS delivers it to both subscriptions. The phone subscription gets the one-line text, and the email subscription gets the full list. The finder sends both from one `publish` call by using SNS's per-protocol message format.

Your text replies take a longer path. The toll-free number passes each reply to the SNS topic `job-replies`, which drops it into the SQS queue `job-replies`. Between checks, the finder long-polls that queue for up to 20 seconds at a time. A queue means the server needs no open port and no web page, so nothing on the internet can reach the finder directly. The queue also keeps a reply for 14 days if the finder is down.

The finder accepts a reply only when its sender is `MY_PHONE`. It deletes every message after reading it.

## Run one Python file on one small server

The whole finder is `finder.py`, written against the Python standard library. Only the optional AWS and Google features import `boto3` and `gspread`. It runs as the systemd service `job-finder` on a $7-a-month Lightsail server with 1 GB of memory and 1 GB of swap.

AWS Lambda was the other option, and it would cost about $0 to $3 a month. The finder runs on a server anyway, for three reasons. The daily watchlist rebuild can run longer than Lambda's 15-minute limit. The `seen` table would have to move from a local SQLite file into S3 or DynamoDB. And the finder ran on the server with no code changes. The server needs an occasional OS update in exchange.

The server uses an IPv4 address because Greenhouse, Lever, and Workday have no IPv6 addresses. It runs in `us-east-1`, close to the US job boards and where SNS text support is most complete.

## Keep each credential narrow

- The finder signs in to AWS as the IAM user `job-finder`. That user can publish to `job-finder` and read and delete messages in `job-replies`, and nothing else. Its key lives only in `~/.aws/credentials` on the server, readable by the `ubuntu` account alone.
- The server allows inbound SSH and nothing else. The SSH key lives only on your Mac.
- The role `job-finder-inbound-texts` lets the texting service publish replies to `job-replies`, and nothing else.
- When the Google Sheet is on, the finder writes job titles as raw text, so a title that starts with `=` never runs as a formula.

## Handle failures one board at a time

Thousands of third-party boards will eventually send something odd: a dropped connection, a cut-off response, a job with no title. `poll` catches any error from one board, logs it as that board's error, and the check goes on. A role with no location still gets reported. A reply the finder cannot read gets logged and deleted, so it never blocks the replies behind it. The time of the last report lives in the `meta` table, so a restart does not send an extra report.

A code review and two adversarial test runs on Nov 13, 2026 found 13 problems. All 13 are fixed, and their repro scripts pass against the current code. `python3 finder.py selftest` covers the main cases, including one full check with a broken board, an empty board, and a repost.

## Known limits

- **A company's first job can go unreported.** If a board had no jobs when the finder first checked it and gives no dates, its first posting only gets recorded.
- **The first check of Microsoft records only its newest 100 US jobs.** Older ones never get reported, which matters little because they are older than a day.
- **The location lists are hand-written.** A foreign city that is missing from `NON_US` and followed by a code that matches a US state reads as US. For example, `Hamburg, DE` reads as Delaware. A job whose primary location is abroad but which also accepts US applicants can be dropped.
