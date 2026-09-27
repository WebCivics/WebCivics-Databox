#!/usr/bin/env python3
"""
Local-only W3C public mailing-list corpus tool.

Fetches HTML archives from lists.w3.org, stores structured records under a
local directory (default: local/w3c-list-corpus/), and supports qualification
reports by name / email / date / keyword context.

NOT for website publishing. Output is gitignored. Be polite to W3C servers:
default delay, resume support, identifiable User-Agent.

Usage examples:
  python scripts/w3c_list_corpus.py fetch --lists public-webid --max-months 1
  python scripts/w3c_list_corpus.py fetch
  python scripts/w3c_list_corpus.py qualify --email timothy.holborn@gmail.com
  python scripts/w3c_list_corpus.py qualify --name "Holborn" --since 2013-01-01
  python scripts/w3c_list_corpus.py stats
"""

from __future__ import annotations

import argparse
import email.utils
import hashlib
import html as html_lib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

DEFAULT_LISTS = (
    "public-webid",
    "public-webpayments",
    "public-credentials",
    "public-rww",
    "public-solid",
    "public-humancentricai",
    "public-schema-gen",
    "public-cogai",
)

BASE = "https://lists.w3.org/Archives/Public"
USER_AGENT = (
    "WebCivics-Databox-local-corpus/0.1 "
    "(+https://github.com/WebCivics/WebCivics-Databox; "
    "local provenance research only; contact via repo)"
)
DEFAULT_DELAY_S = 1.0
DEFAULT_ROOT = Path("local/w3c-list-corpus")

MONTH_HREF_RE = re.compile(
    r'href="(?P<period>\d{4}[A-Za-z]{3})/"[^>]*>\s*(?P<label>[^<]+)',
    re.I,
)
# Message links in month indices (by date / author / subject / thread).
MSG_HREF_RE = re.compile(r'href="(?P<file>\d{1,5}\.html)"', re.I)
# Hypermail 3 embeds headers in HTML comments and <meta> tags.
COMMENT_FIELD_RE = re.compile(
    r'<!--\s*(?P<key>name|email|sent|isosent|subject|id)\s*=\s*"(?P<value>.*?)"\s*-->',
    re.I | re.S,
)
META_RE = re.compile(
    r'<meta\s+name="(?P<key>Author|Subject|Date)"\s+content="(?P<value>.*?)"\s*/>',
    re.I | re.S,
)
HEADER_FROM_RE = re.compile(
    r'<span class="from">.*?<span class="heading">From</span>\s*:\s*(?P<body>.*?)</span>\s*</li>',
    re.I | re.S,
)
HEADER_DATE_RE = re.compile(
    r'<span class="date">.*?<span class="heading">Date</span>\s*:\s*(?P<body>.*?)</span>',
    re.I | re.S,
)
HEADER_MSGID_RE = re.compile(
    r'<span class="message-id">.*?<span class="heading">Message-Id</span>\s*:\s*(?P<body>.*?)</span>',
    re.I | re.S,
)
# Older hypermail generations.
FROM_RE = re.compile(r"<dfn>From</dfn>\s*:\s*(?P<body>.*?)</li>", re.I | re.S)
DATE_RE = re.compile(r"<dfn>Date</dfn>\s*:\s*(?P<body>.*?)</li>", re.I | re.S)
SUBJECT_RE = re.compile(r"<dfn>Subject</dfn>\s*:\s*(?P<body>.*?)</li>", re.I | re.S)
MSGID_RE = re.compile(r"<dfn>Message-ID</dfn>\s*:\s*(?P<body>.*?)</li>", re.I | re.S)
FROM_ALT_RE = re.compile(r"<b>From:</b>\s*(?P<body>.*?)<br", re.I | re.S)
DATE_ALT_RE = re.compile(r"<b>Date:</b>\s*(?P<body>.*?)<br", re.I | re.S)
SUBJECT_ALT_RE = re.compile(r"<b>Subject:</b>\s*(?P<body>.*?)<br", re.I | re.S)
BODY_PRE_RE = re.compile(
    r'<pre[^>]*(?:id="start"|class="[^"]*body[^"]*")[^>]*>(?P<body>.*?)</pre>',
    re.I | re.S,
)
BODY_PRE_ANY_RE = re.compile(r"<pre[^>]*>(?P<body>.*?)</pre>", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
MAILTO_RE = re.compile(r"mailto:([^\"'?#\s>]+)", re.I)
EMAIL_IN_TEXT_RE = re.compile(
    r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}",
    re.I,
)


@dataclass
class MessageRecord:
    list_name: str
    period: str
    file: str
    url: str
    from_raw: str
    from_name: str
    from_email: str
    date_raw: str
    date_iso: str
    subject: str
    message_id: str
    body_text: str
    body_sha256: str
    fetched_at: str


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def http_get(url: str, delay_s: float, retries: int = 4) -> str:
    last_err: Optional[BaseException] = None
    for attempt in range(retries):
        try:
            time.sleep(delay_s if attempt == 0 else delay_s * (attempt + 1))
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                },
            )
            with urllib.request.urlopen(req, timeout=90) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, "replace")
        except urllib.error.HTTPError as err:
            last_err = err
            if err.code in {403, 404}:
                raise
            eprint(f"HTTP {err.code} for {url}; retry {attempt + 1}/{retries}")
        except Exception as err:  # noqa: BLE001 — network resilience
            last_err = err
            eprint(f"Error fetching {url}: {err}; retry {attempt + 1}/{retries}")
    assert last_err is not None
    raise last_err


def decode_obfuscated(text: str) -> str:
    """Decode W3C archive entity obfuscation (e.g. &#x40; / &#0119;)."""
    return html_lib.unescape(text or "")


def strip_html(fragment: str) -> str:
    text = TAG_RE.sub(" ", fragment)
    text = decode_obfuscated(text)
    return WS_RE.sub(" ", text).strip()


def parse_from(raw_html: str) -> tuple[str, str, str]:
    decoded_html = decode_obfuscated(raw_html)
    raw = strip_html(raw_html)
    mailto = MAILTO_RE.search(decoded_html)
    email_addr = ""
    if mailto:
        email_addr = urllib.parse.unquote(mailto.group(1)).strip().lower()
    name, addr = email.utils.parseaddr(raw)
    if addr and not email_addr:
        email_addr = addr.strip().lower()
    if not email_addr:
        found = EMAIL_IN_TEXT_RE.search(decode_obfuscated(raw))
        if found:
            email_addr = found.group(0).lower()
    display = (name or "").strip()
    if not display:
        display = EMAIL_IN_TEXT_RE.sub("", raw)
        display = display.strip(" <>\"'")
    return raw, display, email_addr


def parse_date(raw_html: str) -> tuple[str, str]:
    raw = strip_html(raw_html)
    iso = ""
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        iso = dt.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, IndexError, OverflowError):
        # Fallback: bare YYYY-MM-DD from meta Date
        m = re.match(r"(\d{4}-\d{2}-\d{2})$", raw)
        if m:
            iso = f"{m.group(1)}T00:00:00+00:00"
    return raw, iso


def parse_isosent(value: str) -> str:
    """Hypermail <!-- isosent="20240701030206" --> → ISO-8601 UTC."""
    value = value.strip()
    if re.fullmatch(r"\d{14}", value):
        dt = datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return ""


def extract_comment_fields(html: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for m in COMMENT_FIELD_RE.finditer(html):
        fields[m.group("key").lower()] = decode_obfuscated(m.group("value"))
    return fields


def extract_meta_fields(html: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for m in META_RE.finditer(html):
        fields[m.group("key").lower()] = decode_obfuscated(m.group("value"))
    return fields


def extract_field(html: str, *patterns: re.Pattern[str]) -> str:
    for pattern in patterns:
        m = pattern.search(html)
        if m:
            return m.group("body")
    return ""


def parse_message(list_name: str, period: str, file_name: str, url: str, html: str) -> MessageRecord:
    comments = extract_comment_fields(html)
    metas = extract_meta_fields(html)

    from_name = (comments.get("name") or "").strip()
    from_email = (comments.get("email") or "").strip().lower()
    subject = (comments.get("subject") or metas.get("subject") or "").strip()
    message_id = (comments.get("id") or "").strip()
    date_raw = (comments.get("sent") or "").strip()
    date_iso = parse_isosent(comments.get("isosent") or "")

    if not from_email or not from_name:
        from_html = extract_field(html, HEADER_FROM_RE, FROM_RE, FROM_ALT_RE)
        if from_html:
            _raw, n, e = parse_from(from_html)
            from_name = from_name or n
            from_email = from_email or e
            from_raw = _raw
        else:
            from_raw = ""
            # Meta Author often: "Name (obfuscated@email)"
            author = metas.get("author") or ""
            am = re.match(r"^(?P<name>.*?)\s*\((?P<email>[^)]+)\)$", author)
            if am:
                from_name = from_name or am.group("name").strip()
                from_email = from_email or am.group("email").strip().lower()
            elif author and not from_name:
                from_name = author
    else:
        from_raw = f"{from_name} <{from_email}>" if from_email else from_name

    if not date_raw or not date_iso:
        date_html = extract_field(html, HEADER_DATE_RE, DATE_RE, DATE_ALT_RE)
        if date_html:
            dr, di = parse_date(date_html)
            date_raw = date_raw or dr
            date_iso = date_iso or di
        elif metas.get("date"):
            dr, di = parse_date(metas["date"])
            date_raw = date_raw or dr
            date_iso = date_iso or di
    elif date_raw and not date_iso:
        _, date_iso = parse_date(date_raw)

    if not subject:
        subject_html = extract_field(html, SUBJECT_RE, SUBJECT_ALT_RE)
        if subject_html:
            subject = strip_html(subject_html)
        else:
            # h1 is usually the subject
            hm = re.search(r"<h1[^>]*>(?P<body>.*?)</h1>", html, re.I | re.S)
            if hm:
                subject = strip_html(hm.group("body"))

    if not message_id:
        msgid_html = extract_field(html, HEADER_MSGID_RE, MSGID_RE)
        if msgid_html:
            message_id = strip_html(msgid_html).strip("<>")

    if not from_raw:
        from_raw = f"{from_name} <{from_email}>".strip()

    body_html = ""
    bm = BODY_PRE_RE.search(html) or BODY_PRE_ANY_RE.search(html)
    if bm:
        body_html = bm.group("body")
    body_text = strip_html(body_html) if body_html else ""
    if len(body_text) > 20000:
        body_text = body_text[:20000] + " …[truncated]"

    digest = hashlib.sha256(body_text.encode("utf-8")).hexdigest()
    return MessageRecord(
        list_name=list_name,
        period=period,
        file=file_name,
        url=url,
        from_raw=from_raw,
        from_name=from_name,
        from_email=from_email,
        date_raw=date_raw,
        date_iso=date_iso,
        subject=subject,
        message_id=message_id,
        body_text=body_text,
        body_sha256=digest,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


def list_periods(list_name: str, delay_s: float) -> list[tuple[str, str]]:
    url = f"{BASE}/{list_name}/"
    html = http_get(url, delay_s)
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for m in MONTH_HREF_RE.finditer(html):
        period = m.group("period")
        if period in seen:
            continue
        seen.add(period)
        out.append((period, strip_html(m.group("label"))))
    return out


def month_message_files(list_name: str, period: str, delay_s: float) -> list[str]:
    url = f"{BASE}/{list_name}/{period}/"
    html = http_get(url, delay_s)
    files = sorted({m.group("file") for m in MSG_HREF_RE.finditer(html)}, key=lambda x: int(x.split(".")[0]))
    return files


def messages_path(root: Path, list_name: str, period: Optional[str] = None) -> Path:
    if period:
        return root / "messages" / list_name / f"{period}.jsonl"
    return root / "messages" / list_name


def state_path(root: Path) -> Path:
    return root / "fetch-state.json"


def raw_path(root: Path, list_name: str, period: str, file_name: str) -> Path:
    return root / "raw" / list_name / period / file_name


def load_state(root: Path) -> dict:
    path = state_path(root)
    if not path.exists():
        return {"completed": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(root: Path, state: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    state_path(root).write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_period_records(path: Path, records: Iterable[MessageRecord]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = list(records)
    with path.open("w", encoding="utf-8") as fh:
        for rec in rows:
            fh.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
    return len(rows)


def iter_records(root: Path, lists: Optional[Iterable[str]] = None) -> Iterator[dict]:
    msg_root = root / "messages"
    if not msg_root.exists():
        return
    allow = set(lists) if lists else None
    # Support both legacy single-file and per-period layout.
    for path in sorted(msg_root.rglob("*.jsonl")):
        rel = path.relative_to(msg_root)
        list_name = rel.parts[0] if len(rel.parts) > 1 else path.stem
        if allow is not None and list_name not in allow:
            continue
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                yield json.loads(line)


def cmd_fetch(args: argparse.Namespace) -> int:
    root: Path = args.root
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.txt").write_text(
        "LOCAL ONLY — W3C public mailing-list corpus for Web Civics provenance research.\n"
        "Do not publish, commit, or deploy this directory to the website.\n"
        f"Fetched with {USER_AGENT}\n",
        encoding="utf-8",
    )
    state = load_state(root)
    completed: dict = state.setdefault("completed", {})

    lists = args.lists or list(DEFAULT_LISTS)
    total_new = 0

    for list_name in lists:
        eprint(f"== {list_name} ==")
        try:
            periods = list_periods(list_name, args.delay)
        except Exception as err:  # noqa: BLE001
            eprint(f"Failed to list periods for {list_name}: {err}")
            continue

        # Newest-first from archive page; optionally reverse for chronological crawl.
        if args.oldest_first:
            periods = list(reversed(periods))

        if args.max_months:
            periods = periods[: args.max_months]

        list_done = completed.setdefault(list_name, {})

        for period, label in periods:
            key = period
            if not args.force and list_done.get(key) == "done":
                eprint(f"  skip {period} ({label}) — already done")
                continue
            eprint(f"  fetch {period} ({label})")
            try:
                files = month_message_files(list_name, period, args.delay)
            except Exception as err:  # noqa: BLE001
                eprint(f"  failed month index {period}: {err}")
                list_done[key] = f"error:{err}"
                save_state(root, state)
                continue

            batch: list[MessageRecord] = []
            errors = 0
            for file_name in files:
                url = f"{BASE}/{list_name}/{period}/{file_name}"
                try:
                    html = http_get(url, args.delay)
                    if args.save_raw:
                        rp = raw_path(root, list_name, period, file_name)
                        rp.parent.mkdir(parents=True, exist_ok=True)
                        rp.write_text(html, encoding="utf-8")
                    batch.append(parse_message(list_name, period, file_name, url, html))
                except Exception as err:  # noqa: BLE001
                    errors += 1
                    eprint(f"    fail {file_name}: {err}")
                    if errors > 25:
                        eprint("    too many errors; aborting month")
                        break

            out_path = messages_path(root, list_name, period)
            n = write_period_records(out_path, batch)
            total_new += n
            list_done[key] = "done" if errors == 0 else f"partial:errors={errors}:saved={n}"
            save_state(root, state)
            eprint(f"    saved {n} messages ({errors} errors) -> {out_path}")

            if args.max_messages and total_new >= args.max_messages:
                eprint("Reached --max-messages; stopping.")
                eprint(f"Total new messages this run: {total_new}")
                return 0

    eprint(f"Total new messages this run: {total_new}")
    return 0


def match_record(
    rec: dict,
    *,
    email_needles: list[str],
    name_needles: list[str],
    since: Optional[datetime],
    until: Optional[datetime],
    keyword: Optional[str],
) -> bool:
    if email_needles:
        em = (rec.get("from_email") or "").lower()
        if not any(n in em for n in email_needles):
            # also allow raw From
            raw = (rec.get("from_raw") or "").lower()
            if not any(n in raw for n in email_needles):
                return False
    if name_needles:
        blob = f"{rec.get('from_name','')} {rec.get('from_raw','')}".lower()
        if not any(n in blob for n in name_needles):
            return False
    if since or until:
        iso = rec.get("date_iso") or ""
        if not iso:
            return False
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            return False
        if since and dt < since:
            return False
        if until and dt > until:
            return False
    if keyword:
        kw = keyword.lower()
        hay = f"{rec.get('subject','')} {rec.get('body_text','')}".lower()
        if kw not in hay:
            return False
    return True


def cmd_qualify(args: argparse.Namespace) -> int:
    root: Path = args.root
    email_needles = [e.lower() for e in (args.email or [])]
    name_needles = [n.lower() for n in (args.name or [])]
    if not email_needles and not name_needles and not args.keyword:
        eprint("Provide --email and/or --name and/or --keyword")
        return 2

    since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc) if args.since else None
    until = datetime.fromisoformat(args.until).replace(tzinfo=timezone.utc) if args.until else None

    matches: list[dict] = []
    for rec in iter_records(root, args.lists):
        if match_record(
            rec,
            email_needles=email_needles,
            name_needles=name_needles,
            since=since,
            until=until,
            keyword=args.keyword,
        ):
            matches.append(rec)

    matches.sort(key=lambda r: r.get("date_iso") or "")

    by_list: Counter[str] = Counter(r["list_name"] for r in matches)
    by_email: Counter[str] = Counter((r.get("from_email") or "(unknown)") for r in matches)
    by_name: Counter[str] = Counter((r.get("from_name") or "(unknown)") for r in matches)
    years: Counter[str] = Counter((r.get("date_iso") or "")[:4] or "unknown" for r in matches)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "filters": {
            "email": email_needles,
            "name": name_needles,
            "since": args.since,
            "until": args.until,
            "keyword": args.keyword,
            "lists": args.lists,
        },
        "match_count": len(matches),
        "by_list": dict(by_list),
        "by_email": dict(by_email.most_common(50)),
        "by_display_name": dict(by_name.most_common(50)),
        "by_year": dict(sorted(years.items())),
        "first_date": matches[0].get("date_iso") if matches else None,
        "last_date": matches[-1].get("date_iso") if matches else None,
        "sample_subjects": [
            {
                "date": r.get("date_iso"),
                "list": r.get("list_name"),
                "from_name": r.get("from_name"),
                "from_email": r.get("from_email"),
                "subject": r.get("subject"),
                "url": r.get("url"),
            }
            for r in matches[: args.sample]
        ],
        "all_urls": [r.get("url") for r in matches] if args.include_urls else None,
    }

    out_dir = root / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"qualify-{stamp}.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # Human summary to stdout (no full bodies — keeps agent context honest but bounded).
    print(f"Matches: {len(matches)}")
    print(f"Date range: {report['first_date']} -> {report['last_date']}")
    print(f"By list: {dict(by_list)}")
    print(f"By year: {dict(sorted(years.items()))}")
    print(f"Top emails: {by_email.most_common(10)}")
    print(f"Top names: {by_name.most_common(10)}")
    print(f"Report written: {out_path}")
    if args.export_matches:
        export_path = out_dir / f"matches-{stamp}.jsonl"
        with export_path.open("w", encoding="utf-8") as fh:
            for r in matches:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"Full matches JSONL: {export_path}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    root: Path = args.root
    by_list: Counter[str] = Counter()
    by_email: Counter[str] = Counter()
    missing_email = 0
    missing_date = 0
    total = 0
    for rec in iter_records(root, args.lists):
        total += 1
        by_list[rec["list_name"]] += 1
        em = rec.get("from_email") or ""
        if em:
            by_email[em] += 1
        else:
            missing_email += 1
        if not rec.get("date_iso"):
            missing_date += 1
    print(json.dumps(
        {
            "total_messages": total,
            "by_list": dict(by_list),
            "unique_emails": len(by_email),
            "missing_from_email": missing_email,
            "missing_date_iso": missing_date,
            "top_posters": by_email.most_common(25),
        },
        indent=2,
    ))
    return 0


def cmd_participants(args: argparse.Namespace) -> int:
    """Aggregate distinct participants with first/last seen — for qualification context."""
    root: Path = args.root
    people: dict[str, dict] = {}
    for rec in iter_records(root, args.lists):
        key = (rec.get("from_email") or "").lower() or f"name:{(rec.get('from_name') or '').lower()}"
        if not key or key == "name:":
            continue
        entry = people.setdefault(
            key,
            {
                "from_email": rec.get("from_email"),
                "names": Counter(),
                "lists": Counter(),
                "count": 0,
                "first_date": rec.get("date_iso"),
                "last_date": rec.get("date_iso"),
                "sample_urls": [],
            },
        )
        entry["count"] += 1
        if rec.get("from_name"):
            entry["names"][rec["from_name"]] += 1
        entry["lists"][rec["list_name"]] += 1
        iso = rec.get("date_iso") or ""
        if iso and (not entry["first_date"] or iso < entry["first_date"]):
            entry["first_date"] = iso
        if iso and (not entry["last_date"] or iso > entry["last_date"]):
            entry["last_date"] = iso
        if len(entry["sample_urls"]) < 3 and rec.get("url"):
            entry["sample_urls"].append(rec["url"])

    rows = []
    for key, entry in people.items():
        rows.append(
            {
                "key": key,
                "from_email": entry["from_email"],
                "names": [n for n, _ in entry["names"].most_common(5)],
                "lists": dict(entry["lists"]),
                "count": entry["count"],
                "first_date": entry["first_date"],
                "last_date": entry["last_date"],
                "sample_urls": entry["sample_urls"],
            }
        )
    rows.sort(key=lambda r: (-r["count"], r.get("from_email") or r["key"]))

    out_dir = root / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "participants.json"
    out_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Participants: {len(rows)}")
    print(f"Written: {out_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help=f"Local corpus root (default: {DEFAULT_ROOT})",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="Download list archives into local JSONL")
    f.add_argument("--lists", nargs="*", default=None, help="List short names (default: curated set)")
    f.add_argument("--delay", type=float, default=DEFAULT_DELAY_S, help="Seconds between HTTP requests")
    f.add_argument("--max-months", type=int, default=None, help="Limit months per list (newest first unless --oldest-first)")
    f.add_argument("--max-messages", type=int, default=None, help="Stop after N newly saved messages this run")
    f.add_argument("--oldest-first", action="store_true", help="Crawl oldest months first")
    f.add_argument("--force", action="store_true", help="Re-fetch months marked done (rewrites that month file)")
    f.add_argument("--save-raw", action="store_true", help="Also save raw HTML under raw/ for offline reparse")
    f.set_defaults(func=cmd_fetch)

    q = sub.add_parser("qualify", help="Filter corpus by email/name/date/keyword; write report")
    q.add_argument("--lists", nargs="*", default=None)
    q.add_argument("--email", action="append", default=[], help="Email substring (repeatable)")
    q.add_argument("--name", action="append", default=[], help="Display-name substring (repeatable)")
    q.add_argument("--since", default=None, help="ISO date/time lower bound (UTC)")
    q.add_argument("--until", default=None, help="ISO date/time upper bound (UTC)")
    q.add_argument("--keyword", default=None, help="Match subject+body substring")
    q.add_argument("--sample", type=int, default=30, help="Sample subjects in report")
    q.add_argument("--include-urls", action="store_true", help="Include all match URLs in report")
    q.add_argument("--export-matches", action="store_true", help="Also write full matching JSONL")
    q.set_defaults(func=cmd_qualify)

    s = sub.add_parser("stats", help="Corpus coverage statistics")
    s.add_argument("--lists", nargs="*", default=None)
    s.set_defaults(func=cmd_stats)

    part = sub.add_parser("participants", help="Aggregate participants with first/last dates")
    part.add_argument("--lists", nargs="*", default=None)
    part.set_defaults(func=cmd_participants)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
