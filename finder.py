"""Stage 1 job finder: polls companies' own job boards (Greenhouse, Lever, Ashby,
Workday, SmartRecruiters) and records new entry-level / intern roles in the US.

The watchlist of boards comes from three places, rebuilt daily:
  1. Simplify's public listing history (every company that has posted a new-grad or intern role there)
  2. extra_companies.txt, your own list (always checked every 5 minutes)
  3. every active, public or acquired Y Combinator company, found by guessing its board name on
     Ashby, Greenhouse and Lever
all_companies.txt lists every company, yours first, then A-Z.

Usage:
  python3 finder.py                 # run forever: tick every 5 min
  python3 finder.py --once          # one tick, then exit
  python3 finder.py --once --limit 50 --emit-existing   # quick test on 50 boards
  python3 finder.py selftest        # run the filter checks

Every role that passes the filters gets a number (#214) and is stored in state.db.
Optional features, each turned on by environment variables (pip install boto3 gspread):
  Reports    SNS_TOPIC_ARN    Only when something new was found: the full list by email at most every
                              30 minutes, a short text at most once an hour. Each subscription needs
                              a filter policy on the "to" attribute (see SETUP.md).
  Replies    REPLY_QUEUE_URL  Text back role numbers ("214 219") to pick them for resumes.
             MY_PHONE         Only texts from this number are accepted.
  Tracker    GOOGLE_SHEET_ID  Every role and its status go to this sheet; columns K-M are yours.
             GOOGLE_CREDENTIALS  path to the service account key (default: google_service_account.json)
Picked roles have status 'picked' in the sheet and in state.db; the resume step reads them from there.
"""
import html, http.client, json, os, re, sqlite3, sys, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
DB = HERE / "state.db"
WATCHLIST = HERE / "watchlist.json"
COMPANIES = HERE / "all_companies.txt"
EXTRA = HERE / "extra_companies.txt"
PROBED = HERE / "probed.json"
YC_COMPANIES = "https://yc-oss.github.io/api/companies/all.json"
SIMPLIFY = [
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json",
]
TICK_SECONDS = 300
PRIORITY_COUNT = 300
REST_CYCLE_TICKS = 24
WORKERS = 16
MAX_AGE_DAYS = 1
MIN_SOFTWARE_TERMS = 2
REPOST_WINDOW_DAYS = 90
REPORT_EVERY_SECONDS = {"email": 1800, "sms": 3600}  # the SMS registration promises at most one text an hour
PROBE_PER_DAY = 500
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN")
REPLY_QUEUE_URL = os.environ.get("REPLY_QUEUE_URL")
MY_PHONE = os.environ.get("MY_PHONE")                # replies are only accepted from this number, e.g. +12065550123
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", str(HERE / "google_service_account.json"))
GOOGLE_SHEET_TAB = os.environ.get("GOOGLE_SHEET_TAB", "Job Finder")  # its own tab, so a shared tracker's tabs stay untouched
UA = {"User-Agent": "job-board-watcher/0.1"}

LEVEL_POS = re.compile(
    r"new ?grad|(?:recent|university|college) grad|early[- ]career|entry[- ]level|university|campus|college|junior|\bjr\b"
    r"|\b(?:engineer|developer|scientist|analyst)\s+(?:i|1)\b|\b2027\b|\bintern(?:ship)?s?\b|co-?op"
    r"|apprentice|rotational|associate (?:software|data|machine|ml|ai)|early[- ]in[- ]career|\bsde\s*(?:i|1)\b"
    r"|\bgraduate (?:software|data|machine|ml|ai|engineer|developer|analyst|scientist)", re.I)
LEVEL_NEG = re.compile(
    r"\b(?:senior|sr|staff|principal|lead|manager|director|head|vp|(?<!/)ii|iii|iv|phd|ph\.d|postdoc|mba|2025|2026"
    r"|(?:engineer|developer|scientist|analyst|sde|level)\s+[23]|recruit\w*|sourcer|talent acquisition"
    r"|graduate (?:intern|internship|research|appointee|student)|adjunct|faculty|professor|instructor|lecturer|teacher)\b", re.I)
DOMAIN = re.compile(
    r"software|engineer|developer|programmer|data|machine learning|\bml\b|\bai\b|artificial intelligence"
    r"|scien|quant|research|infrastructure|platform|security|\bsre\b|devops|cloud|back-?end|front-?end"
    r"|full[- ]?stack|mobile|\bios\b|android|systems|firmware|embedded|technical|technology|analytics"
    r"|game|automation|\bqa\b|\btest|\bsde\b", re.I)


def age_days(published, now=None):
    m = re.match(r"Posted (Today|Yesterday|(\d+)\+? Days? Ago)", published or "")  # Workday's wording
    if m:
        return 0 if m.group(1) == "Today" else 1 if m.group(1) == "Yesterday" else int(m.group(2))
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", published or ""):  # a bare date: count calendar days, not hours since midnight UTC
        return float(((now or datetime.now(timezone.utc)).date() - date.fromisoformat(published)).days)
    try:
        t = datetime.fromisoformat((published or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - t).total_seconds() / 86400


def is_recent(published):
    age = age_days(published)
    return age is None or age <= MAX_AGE_DAYS


SOFTWARE_TERMS = re.compile(
    r"(?<!\w)(?:python|java|javascript|typescript|c\+\+|c#|golang|rust|kotlin|swift|scala|ruby|php|sql|nosql|"
    r"react\.?js|react native|angular|vue|node\.?js|django|flask|spring boot|\.net|aws|azure|gcp|google cloud|docker|"
    r"kubernetes|terraform|linux|git|github|ci/cd|graphql|kafka|spark|airflow|pandas|numpy|pytorch|tensorflow|"
    r"scikit-learn|machine learning|software development|software engineering|programming|computer science|"
    r"data structures|algorithms|object[- ]oriented)(?!\w)", re.I)


def detail_url(j):
    """Where a posting's full description lives: the service's job data feed, or the job page itself."""
    board, jid = j["key"].split("#", 1)
    ats, ident = board.split(":", 1)
    if ats == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{ident}/jobs/{jid}"
    if ats == "smartrecruiters":
        return f"https://api.smartrecruiters.com/v1/companies/{j['url'].split('/')[3]}/postings/{jid}"
    if ats == "workday":
        _, _, host, site, rest = j["url"].split("/", 4)
        return f"https://{host}/wday/cxs/{host.split('.')[0]}/{site}/{rest}"
    if ats == "workable":
        return f"https://apply.workable.com/api/v2/accounts/{ident}/jobs/{jid}"
    if ats == "rippling":
        return f"https://api.rippling.com/platform/api/ats/v2/board/{ident}/jobs/{jid}"
    if ats == "pcsx":
        host, domain = ident.split("/")
        return f"https://{host}/api/pcsx/position_details?position_id={jid}&domain={domain}&hl=en"
    return j["url"]


def software_ok(j):
    """True when the job description names at least MIN_SOFTWARE_TERMS software tools or requirements."""
    text = j.get("desc")
    if not text:
        try:
            try:
                text = get_text(detail_url(j))
            except (urllib.error.URLError, OSError):  # one retry for a busy or flaky server
                time.sleep(2)
                text = get_text(detail_url(j))
        except Exception:  # shortcut: an unreadable description passes, so a flaky page never hides a job
            return True
    text = re.sub(r"(?is)<(script|style|noscript)\b(?![^>]*ld\+json)[^>]*>.*?</\1>", " ", text)  # page code, not the job
    text = re.sub(r"<[^>]+>", " ", html.unescape(text))
    if len(text.strip()) < 500:  # shortcut: a page with no description text (a JavaScript app) passes
        return True
    return len({m.lower() for m in SOFTWARE_TERMS.findall(text)}) >= MIN_SOFTWARE_TERMS


def title_ok(title):
    return bool(LEVEL_POS.search(title) and DOMAIN.search(title) and not LEVEL_NEG.search(title))


STATE_CODES = ("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH "
               "NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC").split()
STATE_NAMES = ("alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|"
               "hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|"
               "michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|"
               "new mexico|new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|"
               "rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|virginia|washington|"
               "west virginia|wisconsin|wyoming")
US_CITIES = ("san francisco|seattle|new york city|nyc|boston|austin|chicago|los angeles|bay area|"
             "silicon valley|palo alto|mountain view|sunnyvale|san jose|santa clara|menlo park|redmond|"
             "bellevue|denver|atlanta|pittsburgh|st\\. louis|saint louis|san diego|portland|dallas|houston|"
             "washington dc|miami|philadelphia|raleigh|salt lake city|minneapolis|detroit|irvine|santa monica|"
             "playa vista|san mateo|woodland hills|sherman oaks|carlsbad|middleton|culver city|burbank|"
             "foster city|redwood city")
US_STRONG = re.compile(rf"united[ -]states|\busa\b|\bu\.s\.|\bus\b|\b(?:{US_CITIES}|new york)\b", re.I)
US_STATE = re.compile(rf"\b(?:{STATE_NAMES})\b", re.I)
NON_US = re.compile(
    r"\b(?:india|canada|mexico|brazil|argentina|chile|colombia|peru|costa rica|united kingdom|uk|england|scotland|"
    r"ireland|germany|france|spain|portugal|italy|netherlands|belgium|switzerland|austria|poland|czech\w*|romania|"
    r"hungary|ukraine|sweden|norway|denmark|finland|estonia|lithuania|latvia|greece|turkey|israel|egypt|nigeria|"
    r"kenya|south africa|uae|united arab emirates|saudi arabia|qatar|singapore|malaysia|indonesia|philippines|"
    r"vietnam|thailand|japan|china|taiwan|hong kong|korea|australia|new zealand|pakistan|bangladesh|sri lanka|"
    r"serbia|croatia|bulgaria|emea|apac|latam|europe|london|berlin|munich|paris|amsterdam|stockholm|barcelona|"
    r"madrid|lisbon|zurich|warsaw|krakow|prague|bucharest|kyiv|tel aviv|toronto|montreal|ottawa|calgary|ontario|"
    r"quebec|british columbia|alberta|bengaluru|bangalore|hyderabad|chennai|pune|mumbai|delhi|gurgaon|gurugram|"
    r"noida|tokyo|seoul|shanghai|beijing|shenzhen|sydney|melbourne|manila|s[aã]o paulo|buenos aires|"
    r"mexico city|guadalajara|tbilisi)\b", re.I)
US_CODE = re.compile(rf"(?:^|[,\s-])(?:{'|'.join(STATE_CODES)})(?:$|[,\s-])")
AMBIGUOUS = re.compile(r"\bremote\b|\d+ locations|multiple locations|anywhere|various", re.I)


def us_status(text):
    found = {place_status(p) for p in (text or "").split(";") if p.strip()} or {"unknown"}
    return "us" if "us" in found else "unknown" if "unknown" in found else "non-us"


def place_status(place):
    if US_STRONG.search(place):
        return "us"
    if NON_US.search(place):  # before state names and codes: "Bengaluru, IN" is not Indiana
        return "non-us"
    if US_STATE.search(place) or US_CODE.search(place):
        return "us"
    return "unknown" if AMBIGUOUS.search(place) else "non-us"


US_COUNTRY = {"United States", "United States of America", "USA", "US", "U.S."}


def us_from(country, text):
    """The board's own country field wins. Text decides only when the board gives no country."""
    if country in US_COUNTRY:
        return "us"
    found = us_status(text)
    return "non-us" if country and found == "unknown" else found


BOARD_PATTERNS = [
    ("greenhouse", re.compile(r"greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)")),
    ("lever", re.compile(r"jobs\.lever\.co/([\w.-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)")),
    ("smartrecruiters", re.compile(r"jobs\.smartrecruiters\.com/([\w-]+)")),
    ("breezy", re.compile(r"([\w-]+)\.breezy\.hr")),
    ("rippling", re.compile(r"ats\.rippling\.com/([\w-]+)")),
    ("workable", re.compile(r"apply\.workable\.com/(?!j/|api/)([\w-]+)")),
    ("jobvite", re.compile(r"jobs\.jobvite\.com/([\w-]+)")),
    ("jobscore", re.compile(r"careers\.jobscore\.com/(?:careers|jobs)/([\w-]+)")),
    ("gem", re.compile(r"jobs\.gem\.com/([\w-]+)")),
    ("workday", re.compile(r"([\w-]+\.wd\d+\.myworkdayjobs\.com)/(?:[a-zA-Z]{2}-[a-zA-Z]{2}/)?([\w-]+)")),
]


PROBE_URLS = {
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{}",
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{}/jobs",
    "lever": "https://api.lever.co/v0/postings/{}?mode=json&limit=1",
    "breezy": "https://{}.breezy.hr/json",
}


def add_board(boards, ats, ids, company, source, count=0, priority=False):
    key = f"{ats}:{'/'.join(ids)}".lower()
    b = boards.setdefault(key, {"key": key, "ats": ats, "id": list(ids), "company": company,
                                "source": source, "count": 0, "priority": False})
    b["count"] += count
    b["priority"] |= priority


def board_from_url(url):
    for ats, pat in BOARD_PATTERNS:
        m = pat.search(url)
        if m and not (ats == "greenhouse" and m.group(1) in ("embed", "jobs")):
            return ats, m.groups()
    return None


def slug_candidates(name, website=""):
    base = re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()
    out = [base.replace(" ", ""), re.sub(r"\s+", "-", base)]
    m = re.search(r"//(?:www\.)?([a-z0-9-]+)\.", (website or "").lower())
    if m:
        out.append(m.group(1))
    return list(dict.fromkeys(s for s in out if s))


def board_exists(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
            return r.status == 200
    except Exception:  # shortcut: a failed request counts as "no board" until the next re-check
        return False


def probe(company):
    name, website, slugs = company
    found = [(ats, s) for s in dict.fromkeys(slugs + slug_candidates(name, website))
             for ats, url in PROBE_URLS.items() if board_exists(url.format(s))]
    return name, found


def probe_all(companies):
    cache = json.loads(PROBED.read_text()) if PROBED.exists() else {}
    age = lambda c: cache.get(c[0], {}).get("t", 0)
    todo = sorted((c for c in companies if time.time() - age(c) > 7 * 86400), key=age)[:PROBE_PER_DAY]
    if todo:
        print(f"probing {len(todo)} companies for job boards...", file=sys.stderr)
    with ThreadPoolExecutor(WORKERS) as pool:
        for name, found in pool.map(probe, todo):
            cache[name] = {"t": time.time(), "found": found}
    PROBED.write_text(json.dumps(cache))
    return [(c[0], cache[c[0]]["found"]) for c in companies if c[0] in cache]


def build_watchlist():
    boards = {}
    for url in SIMPLIFY:
        for x in get_json(url):
            hit = board_from_url(x.get("url", ""))
            if hit:
                add_board(boards, *hit, x.get("company_name", ""), "simplify", count=1)
    extras = []
    for line in EXTRA.read_text().splitlines() if EXTRA.exists() else []:
        name, hit = parse_extra(line)
        if hit:
            add_board(boards, *hit, name or hit[1][0], "your list", priority=True)
        elif name:
            extras.append((name, "", []))
    yc = [(c["name"], c.get("website", ""), [c["slug"]]) for c in get_json(YC_COMPANIES)
          if c.get("status") in ("Active", "Public", "Acquired")]
    extra_names = {e[0] for e in extras}
    for name, found in probe_all(extras + yc):
        for ats, slug in found:
            mine = name in extra_names
            add_board(boards, ats, [slug], name, "your list" if mine else "yc", priority=mine)
    out = sorted(boards.values(), key=lambda b: (not b["priority"], -b["count"]))
    WATCHLIST.write_text(json.dumps(out))
    write_company_list(out)
    return out


def parse_extra(line):
    """One line of extra_companies.txt -> (company name, (ats, ids) or None). Three forms:
         Epic Games  https://job-boards.greenhouse.io/epicgames     a standard board link
         Microsoft | pcsx | apply.careers.microsoft.com | microsoft.com   a custom board: name | ats | ids...
         Natoma                                                      a name to look up"""
    line = line.split("#")[0].strip()
    if "|" in line:
        name, ats, *ids = [p.strip() for p in line.split("|")]
        return name, ((ats, tuple(ids)) if ats in FETCHERS and ids else None)
    hit = board_from_url(line) if line else None
    if hit:
        return (line[:line.find("http")].strip() if "http" in line else ""), hit
    return line, None


KNOWN_MISSING = ("Natoma (website offline, no job board). Capcom USA is watched but has had no openings.")


def write_company_list(boards):
    def label(b):
        c = b["company"].strip()
        return c[:1].upper() + c[1:] if c == c.lower() else c
    mine, rest = {}, {}
    for b in boards:
        (mine if b["priority"] else rest).setdefault(label(b), set()).add(b["ats"].capitalize())
    rest = {c: a for c, a in rest.items() if c not in mine}
    entry = lambda c, a: f"{c}  [{', '.join(sorted(a))}]"
    lines = ["ALL COMPANIES THE JOB FINDER CHECKS",
             f"{len(mine) + len(rest)} companies on {len(boards)} job boards. Rebuilt daily.",
             "To add one, put its careers link or name in extra_companies.txt.", "",
             "=== KNOWN MISSING: they use their own careers sites, so each needs custom code ===",
             KNOWN_MISSING, "",
             f"=== YOUR LIST: checked every 5 minutes ({len(mine)}) ===",
             *[entry(c, a) for c, a in sorted(mine.items(), key=lambda x: x[0].lower())], "",
             f"=== EVERYONE ELSE, A-Z ({len(rest)}) ==="]
    letter = None
    for c, a in sorted(rest.items(), key=lambda x: x[0].lower()):
        first = c[:1].upper() if c[:1].isalpha() else "#"
        if first != letter:
            letter = first
            lines += ["", f"--- {letter} ---"]
        lines.append(entry(c, a))
    COMPANIES.write_text("\n".join(lines) + "\n")


def load_watchlist():
    if not WATCHLIST.exists() or time.time() - WATCHLIST.stat().st_mtime > 86400:
        try:
            return build_watchlist()
        except Exception as e:
            print(f"watchlist refresh failed: {e}", file=sys.stderr)
    return json.loads(WATCHLIST.read_text())


def get_json(url, data=None, headers=None):
    headers = dict(UA, **({"Content-Type": "application/json"} if data is not None else {}), **(headers or {}))
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def job(board, jid, title, loc, url, us, pay="", published="", desc=""):
    return {"key": f"{board['key']}#{jid}", "company": board["company"], "title": (title or "").strip(),
            "location": loc or "", "us": us, "pay": pay, "url": url, "published": published, "ats": board["ats"],
            "priority": int(board.get("priority", False)), "listings": board.get("count", 0), "desc": desc}


def fetch_greenhouse(b, seen):
    d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{b['id'][0]}/jobs")
    out = []
    for j in d["jobs"]:
        loc = (j.get("location") or {}).get("name", "")
        b["company"] = j.get("company_name") or b["company"]
        out.append(job(b, j["id"], j["title"], loc, j["absolute_url"], us_status(loc),
                       published=j.get("first_published", "")))
    return out


def fetch_lever(b, seen):
    out = []
    for j in get_json(f"https://api.lever.co/v0/postings/{b['id'][0]}?mode=json"):
        locs = "; ".join(filter(None, j["categories"].get("allLocations") or [j["categories"].get("location")]))
        us = us_from(j.get("country"), locs)
        s = j.get("salaryRange") or {}
        pay = f"{s.get('min')}-{s.get('max')} {s.get('currency', '')}/{s.get('interval', '')}" if s else ""
        out.append(job(b, j["id"], j["text"], locs, j["hostedUrl"], us, pay, desc=json.dumps(j)))  # createdAt is draft time
    return out


def fetch_ashby(b, seen):
    d = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{b['id'][0]}?includeCompensation=true")
    out = []
    for j in d["jobs"]:
        if j.get("isListed") is False:
            continue
        places = [j.get("address")] + [s.get("address") for s in j.get("secondaryLocations") or []]
        countries = {((p or {}).get("postalAddress") or {}).get("addressCountry") for p in places} - {None}
        locs = "; ".join(filter(None, [j.get("location")] + [s.get("location") for s in j.get("secondaryLocations") or []]))
        us = "us" if countries & US_COUNTRY else (us_status(locs) if not countries else "non-us")
        pay = (j.get("compensation") or {}).get("compensationTierSummary") or ""
        out.append(job(b, j["id"], j["title"], locs, j["jobUrl"], us, pay, j.get("publishedAt", ""), json.dumps(j)))
    return out


def fetch_smartrecruiters(b, seen):
    slug = b["id"][0]
    d = get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100")
    out = []
    for j in d.get("content", []):
        loc = j.get("location") or {}
        us = "us" if loc.get("country") == "us" else ("unknown" if loc.get("remote") and not loc.get("country") else "non-us")
        out.append(job(b, j["id"], j["name"], loc.get("fullLocation", ""),
                       f"https://jobs.smartrecruiters.com/{slug}/{j['id']}", us, published=j.get("releasedDate", "")))
    return out


def fetch_workday(b, seen):
    host, site = b["id"]
    api = f"https://{host}/wday/cxs/{host.split('.')[0]}/{site}/jobs"
    out = []
    for offset in range(0, 100, 20):
        page = get_json(api, {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""})
        posts = [j for j in page.get("jobPostings", []) if "externalPath" in j]
        for j in posts:
            path = j["externalPath"]
            loc = f"{j.get('locationsText', '')} | {path.split('/')[2] if path.count('/') > 2 else ''}"
            out.append(job(b, path, j["title"], loc, f"https://{host}/{site}{path}", us_status(loc),
                           published=j.get("postedOn", "")))
        if len(posts) < 20 or (host not in WORKDAY_UNSORTED and
                               any(f"{b['key']}#{j['externalPath']}" in seen for j in posts)):
            break
    return out


WORKDAY_UNSORTED = {"xboxgaming.wd1.myworkdayjobs.com"}


def fetch_breezy(b, seen):
    out = []
    for j in get_json(f"https://{b['id'][0]}.breezy.hr/json"):
        loc = j.get("location") or {}
        country = (loc.get("country") or {}).get("id")
        us = us_from(country, loc.get("name") or ("Remote" if loc.get("is_remote") else ""))
        out.append(job(b, j["id"], j["name"], loc.get("name", ""), j["url"], us, published=j.get("published_date", "")))
    return out


def get_text(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def clean(s):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s)).split())


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else ""


def page_has_seen(b, ids, seen):
    return any(f"{b['key']}#{i}" in seen for i in ids)


# 20 jobs per page whatever jobRecordsPerPage says; jobSort=id DESC puts the newest requisitions first.

def fetch_avature(b, seen):
    base = b["id"][0]
    out = []
    for offset in range(0, 1000, 20):
        page = get_text(f"{base}/SearchJobs/?listFilterMode=1&jobSort=id&jobSortDirection=DESC"
                        f"&jobRecordsPerPage=20&jobOffset={offset}")
        arts = re.findall(r'<article class="article article--result.*?</article>', page, re.S)
        ids = []
        for a in arts:
            m = re.search(r'href="([^"]+/JobDetail/[^"]+/(\d+))"[^>]*>\s*(.*?)\s*</a>', a, re.S)
            if not m:
                continue
            url, jid, title = m.group(1), m.group(2), clean(m.group(3))
            locs = [clean(x) for x in re.findall(r'class="list-item-(?:location|\d+)">(.*?)</span>', a, re.S)]
            loc = "; ".join(dict.fromkeys(locs))
            ids.append(jid)
            out.append(job(b, jid, title, loc, url, us_status(loc)))
        if len(arts) < 20 or page_has_seen(b, ids, seen):
            break
    return out


def fetch_valve(b, seen):
    page = get_text(b["id"][0])
    out = {}
    for jid, title in re.findall(r'href="https://www\.valvesoftware\.com/en/jobs\?job_id=(\d+)">\s*'
                                 r'<h5 class="job_title">\s*(.*?)\s*</h5>', page, re.S):
        out[jid] = job(b, jid, clean(title), "Bellevue, WA", f"https://www.valvesoftware.com/en/jobs?job_id={jid}", "us")
    return list(out.values())


class KeepAlive:
    """One keep-alive HTTPS connection that carries cookies. Eightfold (Microsoft, Netflix) stalls TLS
    handshakes or answers 429 when every request opens a new connection, so page over one connection."""

    def __init__(self, host, pause=0.0):
        self.conn, self.cookie, self.pause = http.client.HTTPSConnection(host, timeout=30), "", pause

    def get_json(self, path):
        for attempt in range(4):
            try:
                self.conn.request("GET", path, headers=dict(UA, Cookie=self.cookie))
                r = self.conn.getresponse()
                body = r.read()
            except (http.client.RemoteDisconnected, ConnectionResetError):
                self.conn.close()
                continue
            cookies = [v.split(";")[0] for k, v in r.getheaders() if k.lower() == "set-cookie"]
            self.cookie = "; ".join(cookies) or self.cookie
            if r.status != 429:
                break
            time.sleep(5 * (attempt + 1))
        else:
            raise ConnectionError("no response after retries")
        if r.status != 200:
            raise ConnectionError(f"HTTP {r.status} for {path}")
        time.sleep(self.pause)
        return json.loads(body)


# Rate limited per IP: faster than ~1 request per 2-3 s gets 429 (it eases off after a minute or so).
# 10 per page (num is ignored); newest first.

def fetch_pcsx(b, seen, max_pages=10):
    host, domain = b["id"]
    s = KeepAlive(host, pause=3)
    out = []
    for page in range(max_pages):  # shortcut: first poll only records the newest 100; older ones are never reported
        posts = s.get_json(f"/api/pcsx/search?domain={domain}&query=&location=United%20States"
                           f"&start={page * 10}&sort_by=timestamp")["data"]["positions"]
        for p in posts:
            loc = "; ".join(filter(None, p.get("standardizedLocations") or p.get("locations") or []))
            out.append(job(b, p["id"], p["name"], loc, f"https://{host}{p['positionUrl']}", us_status(loc),
                           published=iso(p.get("postedTs"))))
        if len(posts) < 10 or page_has_seen(b, [p["id"] for p in posts], seen):
            break
    return out


# num is capped at 10 server-side; sort_by=new is newest first.

def fetch_eightfold(b, seen, max_pages=60):
    host, domain = b["id"]
    s = KeepAlive(host)
    out = []
    for page in range(max_pages):
        d = s.get_json(f"/api/apply/v2/jobs?domain={domain}&start={page * 10}&num=10&sort_by=new")
        posts = d.get("positions", [])
        for p in posts:
            loc = "; ".join(filter(None, p.get("locations") or [p.get("location")]))
            out.append(job(b, p["id"], p["name"], loc, p.get("canonicalPositionUrl") or f"https://{host}/careers/job/{p['id']}",
                           us_status(loc), published=iso(p.get("t_create")), desc=json.dumps(p)))
        if len(posts) < 10 or (page + 1) * 10 >= d.get("count", 0) or page_has_seen(b, [p["id"] for p in posts], seen):
            break
    return out


def fetch_paycom(b, seen):
    key = b["id"][0]
    portal = f"https://www.paycomonline.net/v4/ats/web.php/portal/{key}"
    page = get_text(f"{portal}/career-page")
    jwt = json.JSONDecoder().raw_decode(page[page.index("var configsFromHost = ") + 22:])[0]["sessionJWT"]
    filters = {"distanceFrom": 0, "workEnvironments": [], "positionTypes": [], "educationLevels": [], "categories": [],
               "travelTypes": [], "shiftTypes": [], "otherFilters": [], "keywordSearchText": "", "location": "",
               "sortOption": "N"}
    out = []
    for skip in range(0, 2000, 100):
        d = get_json("https://portal-applicant-tracking.us-cent.paycomonline.net/api/ats/job-posting-previews/search",
                      {"skip": skip, "take": 100, "filtersForQuery": filters}, {"Authorization": jwt, "Locale": "en-US"})
        posts = d.get("jobPostingPreviews") or []
        for j in posts:
            loc = j.get("locations", "")
            out.append(job(b, j["jobId"], j["jobTitle"], loc, f"{portal}/jobs/{j['jobId']}", us_status(loc),
                           published=j.get("postedOn") or ""))
        if skip + 100 >= (d.get("jobPostingPreviewsCount") or 0):
            break
    return out


def fetch_jobvite(b, seen):
    slug = b["id"][0]
    page = get_text(f"https://jobs.jobvite.com/{slug}/jobs")
    out = []
    for jid, title, loc in re.findall(r'<td class="jv-job-list-name">\s*<a href="/[^/]+/job/(\w+)">(.*?)</a>\s*</td>\s*'
                                      r'<td class="jv-job-list-location">(.*?)</td>', page, re.S):
        loc = clean(loc)
        out.append(job(b, jid, clean(title), loc, f"https://jobs.jobvite.com/{slug}/job/{jid}", us_status(loc)))
    return out


def fetch_workable(b, seen):
    acct = b["id"][0]
    out = []
    for j in get_json(f"https://apply.workable.com/api/v1/widget/accounts/{acct}")["jobs"]:
        places = j.get("locations") or [{"city": j.get("city"), "region": j.get("state"), "country": j.get("country"),
                                         "countryCode": ""}]
        loc = "; ".join(", ".join(x for x in (p.get("city"), p.get("region"), p.get("country")) if x) for p in places)
        codes = {p.get("countryCode") for p in places} - {"", None}
        us = "us" if "US" in codes or j.get("country") in US_COUNTRY else (
            "unknown" if j.get("telecommuting") and not codes else us_status(loc))
        out.append(job(b, j["shortcode"], j["title"], loc, j["url"], us, published=j.get("published_on", "")))
    return out


def fetch_jobscore(b, seen):
    slug = b["id"][0]
    out = []
    for j in get_json(f"https://careers.jobscore.com/jobs/{slug}/feed.json")["jobs"]:
        loc = j.get("location", "")
        us = "us" if j.get("country") == "US" else us_status(loc)
        out.append(job(b, j["id"], j["title"], loc, j["detail_url"], us, j.get("formatted_public_compensation", ""),
                       j.get("opened_date") or j.get("created_on", ""), json.dumps(j)))
    return out


def fetch_gem(b, seen):
    out = []
    for j in get_json(f"https://api.gem.com/job_board/v0/{b['id'][0]}/job_posts/"):
        names = [(j.get("location") or {}).get("name")] + [(o.get("location") or {}).get("name") for o in j.get("offices") or []]
        loc = "; ".join(n for n in dict.fromkeys(names) if n)
        out.append(job(b, j["id"], j["title"], loc, j["absolute_url"], us_status(loc),
                       published=j.get("first_published_at", ""), desc=json.dumps(j)))
    return out


def fetch_rippling(b, seen):
    slug = b["id"][0]
    out, page = [], 0
    while True:
        d = get_json(f"https://api.rippling.com/platform/api/ats/v2/board/{slug}/jobs?page={page}&pageSize=1000")
        for j in d["items"]:
            places = j.get("locations") or []
            loc = "; ".join(filter(None, (p.get("name") for p in places)))
            codes = {p.get("countryCode") for p in places} - {None}
            us = "us" if "US" in codes else (us_status(loc) if not codes else "non-us")
            out.append(job(b, j["id"], j["name"], loc, j["url"], us))
        page += 1
        if page >= d.get("totalPages", 1):
            return out


def fetch_phenom(b, seen):
    host = b["id"][0]
    out = []
    for frm in range(0, 2000, 100):
        body = {"lang": "en_us", "deviceType": "desktop", "country": "us", "pageName": "search-results",
                "ddoKey": "refineSearch", "sortBy": "Most recent", "subsearch": "", "from": frm, "jobs": True,
                "counts": False, "all_fields": [], "size": 100, "clearAll": False, "jdsource": "facets",
                "isSliderEnable": False, "pageId": "page20", "siteType": "external", "keywords": "", "global": True,
                "selected_fields": {}, "locationData": {}}
        d = get_json(f"https://{host}/widgets", body)["refineSearch"]
        posts = d["data"]["jobs"]
        for j in posts:
            loc = "; ".join(filter(None, j.get("multi_location") or [j.get("location")]))
            us = "us" if j.get("country") in US_COUNTRY else us_status(loc)
            out.append(job(b, j["jobId"], j["title"], loc, f"https://{host}/us/en/job/{j['jobId']}", us,
                           published=j.get("postedDate", "")))
        if frm + 100 >= d.get("totalHits", 0):
            break
    return out


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
            "smartrecruiters": fetch_smartrecruiters, "workday": fetch_workday, "breezy": fetch_breezy,
            "avature": fetch_avature, "valve": fetch_valve, "pcsx": fetch_pcsx, "eightfold": fetch_eightfold,
            "paycom": fetch_paycom, "jobvite": fetch_jobvite, "workable": fetch_workable, "jobscore": fetch_jobscore,
            "gem": fetch_gem, "rippling": fetch_rippling, "phenom": fetch_phenom}


def open_db(path=DB):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, first_seen INTEGER)")
    db.execute("CREATE TABLE IF NOT EXISTS seeded (board TEXT PRIMARY KEY)")
    db.execute("""CREATE TABLE IF NOT EXISTS roles (
        id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, found_at TEXT, company TEXT, title TEXT,
        location TEXT, pay TEXT, ats TEXT, url TEXT, published TEXT, priority INTEGER, listings INTEGER,
        status TEXT DEFAULT 'new', picked_at TEXT, reported INTEGER DEFAULT 0, sheet_state TEXT DEFAULT 'add')""")
    db.execute("CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value REAL)")
    cols = [c[1] for c in db.execute("PRAGMA table_info(roles)")]
    if "resumes" not in cols:  # added after the first deploy
        db.execute("ALTER TABLE roles ADD COLUMN resumes TEXT")
    if "texted" not in cols:  # texts split from email reports; roles already reported count as texted
        db.execute("ALTER TABLE roles ADD COLUMN texted INTEGER DEFAULT 0")
        db.execute("UPDATE roles SET texted=reported")
        db.commit()
    return db


def board_seen(db, key):
    return {k for (k,) in db.execute("SELECT key FROM seen WHERE key >= ? AND key < ?", (key + "#", key + "$"))}


def poll(board, seen):
    try:
        return board, FETCHERS[board["ats"]](board, seen), None
    except Exception as e:  # one board's bad answer is that board's error, never the whole check's
        return board, [], e


def tick(db, boards, emit_existing=False):
    seen = set().union(*(board_seen(db, b["key"]) for b in boards))
    seeded = {k for (k,) in db.execute("SELECT board FROM seeded")}
    new_matches, errors = [], 0
    with ThreadPoolExecutor(WORKERS) as pool:
        for board, jobs, err in pool.map(lambda b: poll(b, seen), boards):
            if err:
                errors += 1
                print(f"error {board['key']}: {err}", file=sys.stderr)
                continue
            fresh = [j for j in jobs if j["key"] not in seen]
            first_poll = board["key"] not in seeded
            db.executemany("INSERT OR IGNORE INTO seen VALUES (?, ?)", [(j["key"], int(time.time())) for j in fresh])
            if jobs:  # an empty answer may be an outage page, so it doesn't count as the first check
                db.execute("INSERT OR IGNORE INTO seeded VALUES (?)", (board["key"],))
            new_matches += [j for j in fresh if title_ok(j["title"]) and j["us"] != "non-us" and is_recent(j["published"])
                            and (not first_poll or emit_existing or age_days(j["published"]) is not None)]
    now = time.strftime("%Y-%m-%d %H:%M")
    since = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - REPOST_WINDOW_DAYS * 86400))
    candidates, picked = [], set()
    for j in new_matches:
        same_role = (j["company"], j["title"].lower())
        if same_role in picked or db.execute("SELECT 1 FROM roles WHERE company=? AND lower(title)=? AND found_at>=?",
                                             (*same_role, since)).fetchone():
            continue
        picked.add(same_role)
        candidates.append(j)
    with ThreadPoolExecutor(WORKERS) as pool:
        kept = [j for j, ok in zip(candidates, pool.map(software_ok, candidates)) if ok]
    for j in kept:
        db.execute("INSERT OR IGNORE INTO roles (key, found_at, company, title, location, pay, ats, url, published,"
                   " priority, listings) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (j["key"], now, j["company"], j["title"], j["location"], j["pay"], j["ats"], j["url"],
                    j["published"], j["priority"], j["listings"]))
        print(f"NEW  {j['company']} | {j['title']} | {j['location']} | {j['url']}")
    db.commit()
    print(f"{time.strftime('%H:%M:%S')} checked {len(boards)} boards, {len(kept)} new matches, {errors} errors")
    return kept


ROLE_COLS = ["id", "company", "title", "location", "pay", "url", "priority", "listings"]


def notable_first(roles):
    return sorted(roles, key=lambda r: (-r["priority"], -r["listings"], r["id"]))


def report_text(roles):
    interns = sum(bool(re.search(r"\bintern|co-?op", r["title"], re.I)) for r in roles)
    names = list(dict.fromkeys(r["company"] for r in notable_first(roles)))
    ids = sorted(r["id"] for r in roles)
    span = f"#{ids[0]}" if len(ids) == 1 else f"#{ids[0]}-{ids[-1]}"
    for k in (3, 2, 1, 0):  # show fewer names until the text fits one 160-character message
        shown = ", ".join(n[:14] for n in names[:k]) + (f" +{len(names) - k} more" if len(names) > k else "")
        text = (f"JobFinder: {len(roles)} new role{'' if len(roles) == 1 else 's'} {span} ({len(roles) - interns} full-time, "
                f"{interns} intern). Notable: {shown.strip()}. Reply #s for resumes, STOP to end.")
        if len(text) <= 160:
            break
    return text


def report_email(roles):
    rows = [f"#{r['id']}  {r['company']} | {r['title']}\n{(r['location'] or '')[:80]} | {r['pay'] or 'pay not listed'}\n{r['url']}"
            for r in notable_first(roles)]
    sheet = f"\n\nTracker: https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}" if GOOGLE_SHEET_ID else ""
    return report_text(roles) + sheet + "\n\n" + "\n\n".join(rows)


def sns_publish(to, **kwargs):  # to: "email", "sms" or "all", matched by each subscription's filter policy
    try:
        import boto3
        boto3.client("sns", region_name=SNS_TOPIC_ARN.split(":")[3]).publish(
            TopicArn=SNS_TOPIC_ARN, MessageAttributes={"to": {"DataType": "String", "StringValue": to}}, **kwargs)
        return True
    except Exception as e:
        print(f"SNS publish failed: {e}", file=sys.stderr)
        return False


def send_report(db, to):
    col = "reported" if to == "email" else "texted"
    roles = [dict(zip(ROLE_COLS, r)) for r in db.execute(f"SELECT {','.join(ROLE_COLS)} FROM roles WHERE {col}=0")]
    if not roles:
        return False
    short = report_text(roles)
    sent = sns_publish(to, Subject=short.split(" (")[0][:100], Message=report_email(roles) if to == "email" else short)
    if sent:
        db.executemany(f"UPDATE roles SET {col}=1 WHERE id=?", [(r["id"],) for r in roles])
        db.commit()
    return sent


def parse_reply(body):
    msg = json.loads(body)
    if "Message" in msg:  # still wrapped in the SNS envelope (raw message delivery off)
        msg = json.loads(msg["Message"])
    return msg.get("originationNumber", ""), msg.get("messageBody", "")


def pick(db, text):
    ids = sorted({int(x) for x in re.findall(r"\d+", text) if len(x) <= 9})
    if not ids:
        return "JobFinder: Reply with role numbers, like 214 219. Reply STOP to opt out."
    found = db.execute(f"SELECT id, company FROM roles WHERE id IN ({','.join('?' * len(ids))}) ORDER BY id", ids).fetchall()
    now = time.strftime("%Y-%m-%d %H:%M")
    db.executemany("UPDATE roles SET status='picked', picked_at=?, sheet_state=CASE sheet_state WHEN 'add' THEN 'add'"
                   " ELSE 'update' END WHERE id=? AND status='new'", [(now, i) for i, _ in found])
    db.commit()
    missing = [i for i in ids if i not in {f[0] for f in found}]
    msg = ("JobFinder: Picked for resumes: " + ", ".join(f"#{i} {c[:20]}" for i, c in found)) if found else "JobFinder: No roles matched"
    return (msg + (f". Not found: {' '.join(f'#{i}' for i in missing)}" if missing else ""))[:300]


def check_replies(db, wait):
    if not (REPLY_QUEUE_URL and MY_PHONE):
        return False
    try:
        import boto3
        sqs = boto3.client("sqs", region_name=REPLY_QUEUE_URL.split(".")[1])
        msgs = sqs.receive_message(QueueUrl=REPLY_QUEUE_URL, MaxNumberOfMessages=10,
                                   WaitTimeSeconds=max(1, min(20, int(wait)))).get("Messages", [])
        for m in msgs:
            try:
                sender, text = parse_reply(m["Body"])
                if sender == MY_PHONE:
                    sns_publish("all", Message=pick(db, text))
            except Exception as e:  # an unreadable reply must not block the ones behind it
                print(f"skipped a reply: {e}", file=sys.stderr)
            sqs.delete_message(QueueUrl=REPLY_QUEUE_URL, ReceiptHandle=m["ReceiptHandle"])
        if msgs:
            sync_sheet(db)
        return True
    except Exception as e:
        print(f"reading replies failed: {e}", file=sys.stderr)
        return False


SHEET_HEADER = ["ID", "Found", "Company", "Title", "Location", "Pay", "Job board", "Link", "Status", "Picked at",
                "Resumes", "Applied", "Notes"]  # the finder writes A-K; Applied and Notes are yours


def mark(db, role_id, status, resumes=""):
    """Record a role's resume result; the sheet shows it in Status and Resumes."""
    db.execute("UPDATE roles SET status=?, resumes=?, sheet_state=CASE sheet_state WHEN 'add' THEN 'add' ELSE 'update' END"
               " WHERE id=?", (status, resumes, role_id))
    db.commit()


def sync_sheet(db):
    if not GOOGLE_SHEET_ID:
        return
    try:
        import gspread
        book = gspread.service_account(filename=GOOGLE_CREDENTIALS).open_by_key(GOOGLE_SHEET_ID)
        try:
            ws = book.worksheet(GOOGLE_SHEET_TAB)
        except gspread.WorksheetNotFound:
            ws = book.add_worksheet(GOOGLE_SHEET_TAB, rows=1000, cols=len(SHEET_HEADER))
        if not ws.acell("A1").value:
            ws.update(values=[SHEET_HEADER], range_name="A1")
        adds = db.execute("SELECT id, found_at, company, title, location, pay, ats, url, status, picked_at, resumes"
                          " FROM roles WHERE sheet_state='add' ORDER BY id").fetchall()
        if adds:
            # write below the last ID ourselves: append_rows lands under a Sheets table without growing it,
            # so each append overwrote the row the previous one wrote. RAW so a title starting with "=" stays text.
            row = len(ws.col_values(1)) + 1
            if row + len(adds) > ws.row_count:
                ws.add_rows(row + len(adds) - ws.row_count)
            ws.update(values=[[*r[:9], r[9] or "", r[10] or ""] for r in adds], range_name=f"A{row}",
                      value_input_option="RAW")
            db.executemany("UPDATE roles SET sheet_state='ok' WHERE id=?", [(r[0],) for r in adds])
            db.commit()
        for role_id, status, picked_at, resumes in db.execute(
                "SELECT id, status, picked_at, resumes FROM roles WHERE sheet_state='update'").fetchall():
            cell = ws.find(str(role_id), in_column=1)
            if cell:
                ws.update(values=[[status, picked_at or "", resumes or ""]], range_name=f"I{cell.row}:K{cell.row}")
            db.execute("UPDATE roles SET sheet_state='ok' WHERE id=?", (role_id,))
            db.commit()
    except Exception as e:
        print(f"sheet sync failed: {e}", file=sys.stderr)


def main(args):
    if "selftest" in args:
        return selftest()
    db = open_db()
    if args[:1] == ["picks"]:  # roles waiting for resumes, as JSON for the make-resumes command
        cols = ["id", "company", "title", "location", "pay", "url", "published"]
        rows = db.execute(f"SELECT {','.join(cols)} FROM roles WHERE status='picked' ORDER BY id")
        return print(json.dumps([dict(zip(cols, r)) for r in rows], indent=1))
    if args[:1] == ["pick"]:  # pick ROLE_ID...: the same as texting the numbers back
        print(pick(db, " ".join(args[1:])))
        return sync_sheet(db)
    if args[:1] == ["mark"]:  # mark ROLE_ID STATUS [RESUME_SUMMARY]
        mark(db, int(args[1]), args[2], args[3] if len(args) > 3 else "")
        return sync_sheet(db)
    limit = int(args[args.index("--limit") + 1]) if "--limit" in args else None
    last = {to: (db.execute("SELECT value FROM meta WHERE name=?", (f"last_{to}",)).fetchone() or [0.0])[0]
            for to in REPORT_EVERY_SECONDS}
    while True:
        boards = load_watchlist()[:limit]
        priority, rest = boards[:PRIORITY_COUNT], boards[PRIORITY_COUNT:]
        n = int(time.time() // TICK_SECONDS)  # from the clock, so a restart continues the rotation
        batch = priority + rest[n % REST_CYCLE_TICKS::REST_CYCLE_TICKS]
        if "--once" in args:
            batch = boards
        start = time.time()
        tick(db, batch, emit_existing="--emit-existing" in args)
        for to, every in REPORT_EVERY_SECONDS.items():
            if SNS_TOPIC_ARN and (time.time() - last[to] >= every or "--once" in args) and send_report(db, to):
                last[to] = time.time()
                db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (f"last_{to}", last[to]))
                db.commit()
        sync_sheet(db)
        if "--once" in args:
            return
        while (left := start + TICK_SECONDS - time.time()) > 0:
            if not check_replies(db, left):
                time.sleep(left)


def selftest():
    yes = ["Software Engineer, New Grad (2027)", "Software Engineer I", "Machine Learning Engineer Intern - Summer 2027",
           "Early Career Data Scientist", "Junior Backend Developer", "University Grad - Software Engineer",
           "Software Engineering Co-op", "Associate Software Engineer", "Graduate Software Engineer", "SDE I",
           "Software Engineer I/II", "Software Engineer Intern (2 openings)", "Software Engineer, Early in Career"]
    no = ["Senior Software Engineer", "Software Engineer II", "Staff ML Engineer", "Recruiter, University Programs",
          "Software Engineer", "PhD Research Intern", "Engineering Manager, New Grad Hiring",
          "Software Engineer Intern - Summer 2026", "Marketing Intern", "International Sales Engineer, Senior",
          "Cybersecurity Graduate Intern - Summer 2027", "Graduate Research Associate - Summer 2027",
          "Campus Recruiter, Technology", "University Recruiter - Engineering", "Technical Recruiter, Early Careers",
          "Software Engineer 2"]
    for t in yes:
        assert title_ok(t), f"should match: {t}"
    for t in no:
        assert not title_ok(t), f"should not match: {t}"
    us = ["San Francisco, CA", "Remote - US", "US, WA, Remote", "Seattle, San Francisco, New York City",
          "Sunnyvale-California-United-States-of-America", "Beavercreek-OH", "New York, New York"]
    non = ["London, United Kingdom", "Bengaluru, Karnataka", "Toronto, Ontario", "Singapore, Singapore",
           "Tel Aviv, IL", "Toronto, CA", "Berlin, DE", "Bengaluru, IN", "Buenos Aires, AR", "Perth, WA, Australia",
           "Chennai, TN, India", "Tbilisi, Georgia", "Remote - India", "Remote, Canada", "London or Remote"]
    us += ["New York, NY; London, UK", "Atlanta, Georgia", "Remote, Georgia"]
    assert us_from("IN", "Remote") == "non-us" and us_from("CA", "Toronto, CA") == "non-us"
    assert us_from("USA", "") == "us" and us_from(None, "Remote") == "unknown"
    unk = ["Remote", "2 Locations", ""]
    for t in us:
        assert us_status(t) == "us", f"should be US: {t}"
    for t in non:
        assert us_status(t) == "non-us", f"should be non-US: {t}"
    for t in unk:
        assert us_status(t) == "unknown", f"should be unknown: {t}"
    assert board_from_url("https://sierraspace.wd1.myworkdayjobs.com/en-US/Sierra_Space_External_Career_Site/job/x") == (
        "workday", ("sierraspace.wd1.myworkdayjobs.com", "Sierra_Space_External_Career_Site"))
    assert board_from_url("https://intel.wd1.myworkdayjobs.com/en-us/External/job/x") == (
        "workday", ("intel.wd1.myworkdayjobs.com", "External"))
    now = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
    for published, days in [("Posted Today", 0), ("Posted Yesterday", 1), ("Posted 3 Days Ago", 3),
                            ("Posted 30+ Days Ago", 30), ("2026-11-13T01:36:34.993Z", 0.44),
                            ("2026-08-05T10:00:00-04:00", 99.92), ("2026-11-12T12:00:00", 1), ("2026-11-12", 1), ("", None)]:
        got = age_days(published, now)
        assert (got is None and days is None) or abs(got - days) < 0.01, (published, got)
    assert not is_recent("2026-08-05T10:00:00-04:00") and is_recent("Posted Today") and is_recent("")
    assert is_recent("Posted Yesterday") and not is_recent("Posted 2 Days Ago")
    assert is_recent(datetime.now(timezone.utc).isoformat()) and not is_recent("2020-01-01T00:00:00Z")
    db = open_db(":memory:")
    for c, t, prio, count in [("RTX", "Software Engineer I", 0, 728), ("Zingage", "Software Engineer, New Grad", 1, 0),
                              ("NVIDIA", "Software Intern - Summer 2027", 0, 251), ("Leidos", "Junior Developer", 0, 226),
                              ("Some Startup With A Very Long Company Name Inc", "ML Intern", 0, 0)]:
        db.execute("INSERT INTO roles (key, company, title, location, pay, url, priority, listings) VALUES (?,?,?,?,?,?,?,?)",
                   (c, c, t, "San Jose, CA", "", "https://example.com/job", prio, count))
    roles = [dict(zip(ROLE_COLS, r)) for r in db.execute(f"SELECT {','.join(ROLE_COLS)} FROM roles")]
    msg = report_text(roles)
    assert msg == ("JobFinder: 5 new roles #1-5 (3 full-time, 2 intern). Notable: Zingage, RTX, NVIDIA +2 more. "
                   "Reply #s for resumes, STOP to end."), msg
    roles = [dict(r, company="A Company Name Way Over Twenty Chars " + str(r["id"])) for r in roles * 200]
    assert len(report_text(roles)) <= 160, len(report_text(roles))
    big = [dict(r, id=r["id"] + 99990) for r in roles]  # 6-digit role numbers
    assert len(report_text(big)) <= 160, len(report_text(big))
    email = report_email([dict(zip(ROLE_COLS, r)) for r in db.execute(f"SELECT {','.join(ROLE_COLS)} FROM roles")])
    assert email.index("#2  Zingage | Software Engineer") < email.index("#1  RTX | Software Engineer I"), email
    assert email.count("https://example.com/job") == 5 and "pay not listed" in email
    inner = {"originationNumber": "+12065550123", "destinationNumber": "+18445550000", "messageBody": "2, 4 and 99"}
    assert parse_reply(json.dumps(inner)) == ("+12065550123", "2, 4 and 99")
    assert parse_reply(json.dumps({"Type": "Notification", "Message": json.dumps(inner)}))[1] == "2, 4 and 99"
    assert pick(db, "2, 4 and 99") == "JobFinder: Picked for resumes: #2 Zingage, #4 Leidos. Not found: #99"
    assert db.execute("SELECT id FROM roles WHERE status='picked'").fetchall() == [(2,), (4,)]
    assert pick(db, "thanks!") == "JobFinder: Reply with role numbers, like 214 219. Reply STOP to opt out."
    assert pick(db, "123456789012345678901") == pick(db, "thanks!")
    mark(db, 2, "resumes ready", "B 84 interview; A 71; D 64; benchmark C 88")
    assert db.execute("SELECT status, resumes FROM roles WHERE id=2").fetchone() == (
        "resumes ready", "B 84 interview; A 71; D 64; benchmark C 88")
    filler = " Our team builds great things together." * 20
    swe = {"key": "x:y#1", "url": "u", "desc": "<p>You will write Python and SQL services.</p>" + filler}
    assert software_ok(swe)
    assert not software_ok(dict(swe, desc="<p>Operate a weed trimmer and change air filters.</p>" + filler))
    assert not software_ok(dict(swe, desc="<script>var javascript = 'python sql';</script><p>Answer phones.</p>" + filler))
    assert software_ok(dict(swe, desc="<div id=root></div>"))  # a JavaScript app with no text: let it through
    assert not title_ok("Mathematics for Elementary Teachers I - Adjunct Faculty - Traditional Campus")
    selftest_tick()
    assert slug_candidates("Scale AI", "https://scale.com") == ["scaleai", "scale-ai", "scale"]
    assert board_from_url("https://jobs.ashbyhq.com/zingage") == ("ashby", ("zingage",))
    assert board_from_url("https://beli.breezy.hr/p/7faefc00059e-data-science-intern") == ("breezy", ("beli",))
    assert board_from_url("https://ats.rippling.com/riot-platforms-careers/jobs") == ("rippling", ("riot-platforms-careers",))
    assert board_from_url("https://apply.workable.com/j/ABC123") is None  # a job link without the account
    assert parse_extra("EA | avature | https://jobs.ea.com/en_US/careers  # comment") == (
        "EA", ("avature", ("https://jobs.ea.com/en_US/careers",)))
    assert parse_extra("Typo | nosuchats | x") == ("Typo", None) and parse_extra("Natoma") == ("Natoma", None)
    assert us_status("Irvine - Blizzard - Blizzard Way | Irvine---Blizzard---Blizzard-Way") == "us"
    assert us_status("London - King - Ampersand | London---King---Ampersand") == "non-us"
    assert board_from_url("https://boards.greenhouse.io/embed/job_app?for=affirm&token=1") == ("greenhouse", ("affirm",))
    print("selftest passed")


def selftest_tick():
    """One board's first check reports only recently dated jobs; repeats and broken or empty boards are handled."""
    import contextlib, io
    jobs = {}

    def fetch_fake(b, seen):
        if b["id"][0] == "broken":
            raise TypeError("odd data")
        return [job(b, *j) for j in jobs.get(b["id"][0], [])]
    FETCHERS["fake"] = fetch_fake
    board = lambda name: {"key": f"fake:{name}", "ats": "fake", "id": [name], "company": name, "priority": 0, "count": 0}
    now = datetime.now(timezone.utc).isoformat()
    db = open_db(":memory:")
    run = lambda: [f"{j['company']}: {j['title']}" for j in tick(db, [board("acme"), board("empty"), board("broken")])]
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        jobs["acme"] = [(1, "Software Engineer I", "Austin, TX", "u", "us", "", "2026-01-01T00:00:00Z"),
                        (2, "Data Science Intern", "Austin, TX", "u", "us", "", now),
                        (3, "Backend Engineer, New Grad", "Austin, TX", "u", "us")]
        first = run()
        jobs["acme"] += [(4, "Software Engineer, New Grad", "New York, NY", "u", "us", "", now),
                         (5, "Software Engineer, New Grad", "Boston, MA", "u", "us", "", now)]
        jobs["empty"] = [(9, "Software Engineer I", "Austin, TX", "u", "us")]
        second = run()
    assert first == ["acme: Data Science Intern"], first
    assert second == ["acme: Software Engineer, New Grad"], second
    del FETCHERS["fake"]
    # email and texts each report every role once, on their own schedule
    global sns_publish
    real, sent = sns_publish, []
    sns_publish = lambda to, **kw: sent.append(to) or True
    try:
        assert send_report(db, "email") and not send_report(db, "email")
        assert send_report(db, "sms") and not send_report(db, "sms") and sent == ["email", "sms"], sent
    finally:
        sns_publish = real


if __name__ == "__main__":
    main(sys.argv[1:])
