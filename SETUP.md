# Job finder setup

Each part works on its own. Without any of it, the finder still runs and prints new roles.

## 1. Lightsail server

- Create a Linux instance (Ubuntu) on the **$7/month plan with public IPv4**. The cheaper IPv6-only plans won't work, because Greenhouse, Lever and Workday have no IPv6 addresses.
- Copy this folder to `/home/ubuntu/job_finder`, then:

```bash
sudo apt-get update && sudo apt-get install -y python3-venv
python3 -m venv ~/venv && ~/venv/bin/pip install boto3 gspread
~/venv/bin/python /home/ubuntu/job_finder/finder.py selftest
```

## 2. Text and email reports (AWS, region us-east-1)

**Status on Nov 13, 2026.** Everything below exists except the phone subscription. That includes the $15 budget
alert `job-finder-monthly`, both topics, the `job-replies` queue with raw delivery, the confirmed email subscription,
the `job-finder` user and its access key on the server, and a toll-free number with two-way
texting. The number waits on its registration. Your phone gets added once AWS approves it.

1. **Phone number.** In *AWS End User Messaging SMS*, request a **US toll-free number** with SMS and **two-way messaging** turned on (about $2/month). Submit its toll-free registration and wait for approval. Nothing can be texted until it's approved.
2. **Report topic.** In *SNS*, create a standard topic `job-finder`. Add two subscriptions: **SMS** to your phone and **Email** to your address (click the confirmation link it emails you). Give each one a subscription filter policy on message attributes, or your phone also gets the 30-minute email reports: `{"to": ["sms", "all"]}` for SMS and `{"to": ["email", "all"]}` for email.
3. **Reply topic and queue.** Create a second SNS topic `job-replies` (it must be separate from `job-finder`), and an *SQS* standard queue `job-replies` subscribed to it. On the phone number's two-way settings, set the destination to the `job-replies` topic.
4. **Access key for the server.** In *IAM*, create a user `job-finder` with only this policy, then create an access key for it and run `aws configure` on the server (or put it in `~/.aws/credentials`):

```json
{"Version": "2012-10-17", "Statement": [
  {"Effect": "Allow", "Action": "sns:Publish", "Resource": "arn:aws:sns:us-east-1:ACCOUNT_ID:job-finder"},
  {"Effect": "Allow", "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage"],
   "Resource": "arn:aws:sqs:us-east-1:ACCOUNT_ID:job-replies"}]}
```

## 3. Google Sheet tracker

1. In *Google Cloud Console*, create a project, enable the **Google Sheets API**, create a **service account**, and download a **JSON key**. Save it on the server as `job_finder/google_service_account.json`.
2. Create a Google Sheet and **share it with the service account's email** (it ends in `iam.gserviceaccount.com`) as Editor.
3. The sheet ID is the long part of its URL: `docs.google.com/spreadsheets/d/<SHEET_ID>/edit`.

The finder writes columns A-J (ID through Picked at) and never touches K-M (Resumes, Applied, Notes), so those are yours.

## 4. Run it all the time

Save as `/etc/systemd/system/job-finder.service`, filling in your values:

```ini
[Unit]
Description=Job finder
After=network-online.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/job_finder
ExecStart=/home/ubuntu/venv/bin/python -u finder.py
Restart=always
RestartSec=30
Environment=SNS_TOPIC_ARN=arn:aws:sns:us-east-1:ACCOUNT_ID:job-finder
Environment=REPLY_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/ACCOUNT_ID/job-replies
Environment=MY_PHONE=+1XXXXXXXXXX
Environment=GOOGLE_SHEET_ID=SHEET_ID

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now job-finder
journalctl -u job-finder -f    # watch it work
```

The first run reads every board once to record what's already open (about 10 minutes, no reports). After that you get an email at most every 30 minutes and a text at most once an hour, and only when something new turns up.

## Picking roles

Each role in the text and email has a number. Reply to the text with the numbers you want (`214 219`). You'll get a confirmation, and those rows switch to **picked** in the sheet. That's where the resume step will start from.
