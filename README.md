# GitHub Profile Automation

A small, careful GitHub Actions bot that **discovers GitHub profiles** with the
official search API and **follows a capped number of them per run** from an
account you own and are authorized to automate. Every confirmed follow is
logged to `data/users_follows.txt` with an ISO 8601 UTC timestamp.

> **Please use it responsibly.** GitHub's Acceptable Use Policies prohibit
> excessive automated bulk activity and inauthentic interactions. Mass or
> follow-for-follow campaigns can get an account rate limited or restricted.
> The defaults (5 follows every 6 hours, hard ceiling of 50 per run) are
> deliberately small. You are responsible for how you configure and use this.

## 1. Overview and architecture

```
search users ──► discovery ──► eligibility ──► follow service ──► history file
 (GitHub API)    (find, dedupe,  (type, age,     (already followed?    (append-only
                  skip history)   exclusions)     PUT follow, 204)      text file)
```

| Module | Job |
| --- | --- |
| `src/config.py` | Reads and validates environment variables; keeps the token out of `repr`. |
| `src/github_client.py` | HTTP calls, timeouts, retries/backoff, rate-limit handling, typed errors. |
| `src/discovery.py` | Lazy paginated search, dedupe by account ID, skips self and history. |
| `src/eligibility.py` | Explainable accept/reject rules. |
| `src/follow_service.py` | The follow pipeline, limits, and safe-stop logic. |
| `src/history.py` | Reads/appends `users_follows.txt`, UTC timestamp formatting. |
| `src/main.py` | Wiring, logging, signal handling, summary, exit codes. |

## 2. Repository structure

```
.github/workflows/auto-follow.yml   workflow (schedule + manual)
src/                                application code (see table above)
tests/                              unit tests, all GitHub calls mocked
data/users_follows.txt              persistent follow history
requirements.txt                    runtime dependency (requests)
requirements-dev.txt                adds pytest for local testing
pytest.ini  .gitignore  .gitattributes  LICENSE  README.md
```

## 3. GitHub token setup

**`GITHUB_TOKEN` vs. your own token.** The built-in `GITHUB_TOKEN` that Actions
creates for every run belongs to a *GitHub App installation* (a bot) and is
scoped to the repository. It can commit files, but it **cannot follow users**.
Following needs a token that belongs to *a real user account*: a **personal
access token (PAT)** you create yourself. The bot follows as that account.
This project uses both: `GITHUB_TOKEN` (automatic) to commit the history file,
and your PAT (secret `GH_TOKEN`) to follow.

Create the PAT while signed in to the account that should do the following:

1. GitHub → **Settings → Developer settings → Personal access tokens**.
2. Either:
   * **Fine-grained token** (preferred): *Account permissions → Followers →
     Read and write*. No repository access is needed. Set a short expiration.
   * **Classic token**: tick only the **`user:follow`** scope.
3. Copy the token once, then store it as a secret (next section). Never paste
   it in code, issues, logs, or chat.

## 4. Required token permissions

| Operation | Endpoint | Needs |
| --- | --- | --- |
| Who am I | `GET /user` | any valid token |
| Discover | `GET /search/users` | any valid token (higher rate limit when authenticated) |
| Profile details | `GET /users/{username}` | public data, no extra permission |
| Check following | `GET /user/following/{username}` | any valid token |
| Follow | `PUT /user/following/{username}` | **Followers: write** (fine-grained) / **`user:follow`** (classic) |

If following fails with a 403/404 for every user, the token almost certainly
lacks that permission; the run aborts after a few consecutive failures.

## 5. GitHub Actions configuration (exact steps)

1. Create a new repository (private is fine) and push this project to it.
2. **Settings → Secrets and variables → Actions → Secrets → New repository
   secret**: name `GH_TOKEN`, value = your PAT.
3. **Settings → Actions → General → Workflow permissions**: leave on the
   default; the workflow declares its own `contents: write` for the job.
   (If your organization forces read-only, choose "Read and write".)
4. *(Optional)* **Variables** tab → add any of: `MAX_FOLLOWS`, `SEARCH_QUERY`,
   `SEARCH_LANGUAGE`, `MIN_ACCOUNT_AGE_DAYS`, `EXCLUDED_USERS`, `DRY_RUN`,
   `HISTORY_BRANCH`.
5. **Actions** tab → **Auto Follow** → **Run workflow** → tick **Dry run**
   for your first run and read the log. Then run it for real.

No per-profile approval exists. If you attach the job to a protected
*Environment*, GitHub's own approval rules for that environment still apply
and are never bypassed.

## 6. Scheduled and manual execution

* **Schedule:** `cron: "17 */6 * * *"` (every 6 hours at minute 17, UTC). Edit
  the cron line in `auto-follow.yml` to change it.
* **Manual:** Actions → Auto Follow → Run workflow, with optional `max_follows`
  and `dry_run` inputs.
* A `concurrency` group prevents overlapping runs; the job times out after
  15 minutes, and the app also stops itself after `MAX_RUNTIME_SECONDS` (600).

## 7. Per-run attempt limit

Priority: manual-run input → repository variable `MAX_FOLLOWS` → default **5**.
Allowed range 1-50 (hard ceiling in `src/config.py`). An *attempt* is a follow
request that was actually sent; already-followed accounts do not count.

## 8. How discovery works

* Query = `SEARCH_QUERY` (default `type:user repos:>=1`) + optional
  `language:<SEARCH_LANGUAGE>` + `created:<cutoff>` derived from
  `MIN_ACCOUNT_AGE_DAYS` (default 30). Sorted by `joined`, newest first.
* Pages of 100, at most `MAX_SEARCH_PAGES` (default 3; the API exposes only the
  first 1000 results). Pages are fetched lazily, only when more candidates are needed.
* Deduplicated by numeric account ID; your own account and everything already in the
  history file are skipped.
* Eligibility: allowed account types (`ACCOUNT_TYPES`, default `user`),
  `EXCLUDED_USERS`, minimum account age, and profile availability (a 404 on the
  profile means deleted or suspended). Only public, documented fields are used.
  Every skip is logged with a short reason.

### All settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `GH_TOKEN` | *(required)* | Personal access token (secret). |
| `MAX_FOLLOWS` | 5 | Follow attempts per run (1-50). |
| `SEARCH_QUERY` | `type:user repos:>=1` | GitHub user-search query. |
| `SEARCH_LANGUAGE` | *(none)* | Adds `language:<x>`. |
| `SEARCH_SORT` / `SEARCH_ORDER` | `joined` / `desc` | `joined`, `followers`, `repositories`, `best-match`. |
| `MAX_SEARCH_PAGES` | 3 | Search pages per run (1-10). |
| `ACCOUNT_TYPES` | `user` | Comma list of `user`, `organization`, `bot`. |
| `MIN_ACCOUNT_AGE_DAYS` | 30 | 0 disables. |
| `EXCLUDED_USERS` | *(none)* | Comma/space separated usernames. |
| `DRY_RUN` | false | Log only; follow nobody; write no history. |
| `HISTORY_FILE` | `data/users_follows.txt` | History location. |
| `FOLLOW_DELAY` | 2 | Seconds between follow requests. |
| `REQUEST_TIMEOUT` / `MAX_RETRIES` | 15 / 3 | HTTP timeout and bounded retries. |
| `MAX_RATE_LIMIT_WAIT` | 60 | Longest rate-limit wait accepted; otherwise the run stops. |
| `MAX_RUNTIME_SECONDS` | 600 | Internal time budget. |
| `MAX_CONSECUTIVE_FAILURES` | 5 | Abort threshold. |

## 9. History persistence

One line per confirmed follow:

```
2026-10-10T16:30:00Z | username | https://github.com/username
```

* Written only after GitHub answers **204**, immediately (flushed + fsync'd), so
  an interrupted run loses nothing.
* Append-only; the file is re-read before each append so duplicates are never written.
* Blank lines and `#` comments are ignored; malformed lines are logged and skipped.
* The workflow commits the file (`if: always()`, only when it changed) and retries
  `pull --rebase` + `push`. `.gitattributes` marks the file `merge=union`, and the
  `concurrency` group prevents two runs from overlapping.
* **Protected default branch?** Create a branch (e.g. `automation`) from it, set
  the repository variable `HISTORY_BRANCH=automation`, and the workflow will check out
  and push to that branch. Merge code changes into it yourself. The bot never bypasses
  branch protection, required reviews, or any policy.
* **When to move beyond a text file:** if the history grows past tens of thousands of
  lines, you need to store more fields (failures, reasons), or several writers
  need it at once, switch to JSONL (still append-only, easier to parse) and then to
  SQLite or a database.

## 10. Error handling and troubleshooting

| Symptom | Meaning / fix |
| --- | --- |
| `GH_TOKEN is not set` (exit 2) | Add the repository secret. |
| `looks like ... installation token` | You supplied `GITHUB_TOKEN`; use a PAT. |
| `GitHub rejected the token (401)` | Token expired or revoked; create a new one. |
| `Forbidden (403)` / repeated `Follow returned 404` | Token lacks Followers/`user:follow` permission. |
| `rate limit` warnings, run ends early | Normal and safe. Next scheduled run continues. |
| `Could not push the history update` | Branch protection; use `HISTORY_BRANCH`. |
| `No more candidates` | Everything found was already processed; change the query. |

Exit codes: `0` ok (including rate-limit/time-limit stops), `1` failure, `2` bad config, `130` cancelled.

## 11. API rate limits and automation constraints

Primary limits: 5,000 requests/hour for a user token; the search API allows about
30 requests/minute. GitHub also applies *secondary* limits to rapid mutating
requests, so the bot pauses between follows. It honours `Retry-After` and
`X-RateLimit-Reset`, retries only transient errors (network, 500/502/503/504) with
exponential backoff (1s, 2s, 4s, ... capped at 30s), and **stops** instead of
waiting when the required wait exceeds `MAX_RATE_LIMIT_WAIT`. It uses exactly one
token and never tries to work around limits.

## 12. Security considerations

* The token is read from `GH_TOKEN`, never hard-coded, written to disk, or logged.
  It is excluded from `repr()`, error messages are built from status codes and
  GitHub's message only, and a log filter masks it as a last line of defence.
* Least privilege: workflow permissions are `{}` globally and `contents: write`
  for the one job; the PAT needs only follow permission and no repository access.
* Only public profile fields are used; the history stores username, URL and time.
* Action versions are pinned to major tags (`@v4`, `@v5`); pin to full commit SHAs
  if your threat model requires it.
* Prefer a fine-grained token with an expiration date and rotate it regularly.

## 13. Local development and testing

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest                      # all GitHub calls are mocked; nobody is ever followed

# Optional real run, with a dry run first:
export GH_TOKEN="<your PAT>"   # keep it out of shell history / files
DRY_RUN=true python -m src.main
```

## 14. Known limitations

* Accounts you already follow, or that fail an eligibility rule, are not written to
  history (only confirmed follows are), so they are re-checked on later runs.
* The search API caps results at 1,000 per query, so a narrow query can run dry.
* Account age is only checked when GitHub provides `created_at`.
* GitHub disables scheduled workflows in public repositories after 60 days
  without repository activity; re-enable them in the Actions tab if that happens.
* Follow requests are not unfollowed or reconciled; this tool only adds follows.
* The code was written against GitHub's REST API (version `2022-11-28`) from
  existing knowledge. Re-verify endpoints and permission names against the current
  docs at https://docs.github.com/en/rest before relying on it.
