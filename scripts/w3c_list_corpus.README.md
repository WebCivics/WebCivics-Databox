# W3C list corpus (local only)

Python tooling to download **public** W3C mailing-list HTML archives into a **local**
corpus for provenance qualification (names, emails, dates, message context).

**Do not publish this data on the website.** The default output directory is
`local/w3c-list-corpus/`, which is gitignored.

## Why

Website provenance claims about early WebID / credentials / RWW→Solid participation
must be checkable against the actual archive heritage — not only recent posts or an
AI agent’s memory. This tool fetches the public HTML archives, normalises From/Date/
Subject fields, and supports qualification reports.

W3C **mbox** downloads for public lists are member-restricted; this crawler uses the
public Hypermail HTML pages with a polite delay and resume state.

## Default lists

- `public-webid`
- `public-webpayments` (credentials heritage)
- `public-credentials`
- `public-rww`
- `public-solid` (successor of RWW)
- `public-humancentricai` (established / chaired by Timothy Holborn)
- `public-schema-gen` (co-chaired)
- `public-cogai`

## Commands

```powershell
# Smoke test: one newest month on one list
python scripts/w3c_list_corpus.py fetch --lists public-webid --max-months 1

# Full curated set (long-running; resume-safe)
python scripts/w3c_list_corpus.py fetch --delay 1.0

# Chronological crawl, limited for testing
python scripts/w3c_list_corpus.py fetch --lists public-rww --oldest-first --max-months 2

# Qualify one participant
python scripts/w3c_list_corpus.py qualify --email timothy.holborn@gmail.com --export-matches --include-urls

python scripts/w3c_list_corpus.py qualify --name Holborn --since 2013-01-01

# Coverage + participant index
python scripts/w3c_list_corpus.py stats
python scripts/w3c_list_corpus.py participants
```

## Output layout

```text
local/w3c-list-corpus/
  README.txt
  fetch-state.json
  messages/<list>/<YYYYMmm>.jsonl
  raw/<list>/<YYYYMmm>/*.html     # optional, with --save-raw
  reports/qualify-*.json
  reports/matches-*.jsonl
  reports/participants.json
```

Each message record includes: list, period, url, from_raw / from_name / from_email,
date_raw / date_iso, subject, message_id, body_text (truncated for size), body_sha256.

## Ethics / ops

- Identifiable User-Agent; default ≥1s between requests.
- Public archives only; no Member/Team lists.
- Corpus stays on the machine used for research; not copied into `docs/`.
- Re-running `fetch` without `--force` skips months marked `done`.
- `--force` **appends** again (can duplicate); prefer a clean root if re-scraping.

## Using with an agent

After a fetch + qualify export, point the agent at:

- `local/w3c-list-corpus/reports/qualify-*.json`
- optionally `matches-*.jsonl` for message context

Those files are evidence. The agent should not invent participation claims that
contradict empty qualify results, and should cite archive URLs from the reports.
