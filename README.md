# Job finder

`finder.py` finds new US entry-level and intern software roles within minutes of a company posting them. It reads companies' own job boards instead of the public new-grad lists, which trailed company postings by a median of 6.3 hours in a Nov 2026 sample.

It checks about 5,100 job boards on 17 kinds of job-board services, including Greenhouse, Lever, Ashby, Workday, and the custom careers sites of EA, Microsoft, Netflix, and Valve. It runs as one Python file on a $7-a-month server, texts and emails a summary at most once an hour, and lets you text back the roles you want.

## Docs

- [ARCHITECTURE.md](ARCHITECTURE.md) explains how it decides what is new and why it is built this way.
- [SETUP.md](SETUP.md) builds the server and the AWS pieces from scratch.
- [USER_GUIDE.md](USER_GUIDE.md) covers everyday tasks: reading reports, picking roles, adding companies, and deploying changes.
- [REFERENCE.md](REFERENCE.md) lists settings, files, message formats, and resources.

## Try it

```bash
python3 finder.py selftest
python3 finder.py --once --limit 50
```

The first command runs the built-in checks and makes no network requests. The second checks the first 50 boards once and prints what it finds. It needs Python 3.11 or later and nothing else. Reports, text replies, and the Google Sheet tracker need `boto3` and `gspread`, and turn on through environment variables listed in [REFERENCE.md](REFERENCE.md).
