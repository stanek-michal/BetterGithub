"""Background sync of GitHub data into the local SQLite cache."""
import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from html import unescape

from . import db
from .config import Config, author_key, matches_any
from .github import GitHub

log = logging.getLogger("bgh.sync")

# ---------------------------------------------------------------- GraphQL bits

ACTOR = "login __typename"

PR_LIST_FIELDS = f"""
number title url state isDraft createdAt updatedAt closedAt
author {{ {ACTOR} }}
labels(first: 30) {{ nodes {{ name color }} }}
assignees(first: 20) {{ nodes {{ login }} }}
reviewRequests(first: 20) {{ nodes {{ requestedReviewer {{
  __typename ... on User {{ login }} ... on Team {{ slug }} ... on Bot {{ login }} ... on Mannequin {{ login }}
}} }} }}
reviewDecision headRefOid baseRefName headRefName additions deletions changedFiles
comments {{ totalCount }}
commits(last: 1) {{ nodes {{ commit {{ statusCheckRollup {{ state }} }} }} }}
"""

ISSUE_LIST_FIELDS = f"""
number title url state createdAt updatedAt closedAt
author {{ {ACTOR} }}
labels(first: 30) {{ nodes {{ name color }} }}
assignees(first: 20) {{ nodes {{ login }} }}
comments {{ totalCount }}
"""

TIMELINE_FRAGMENTS = f"""
__typename
... on IssueComment {{ id author {{ {ACTOR} }} bodyHTML createdAt url }}
... on PullRequestReview {{ id author {{ {ACTOR} }} state bodyHTML createdAt submittedAt url commit {{ oid }} }}
... on PullRequestCommit {{ id commit {{ oid abbreviatedOid messageHeadline committedDate
     author {{ name user {{ login }} }} }} }}
... on HeadRefForcePushedEvent {{ id actor {{ {ACTOR} }} createdAt beforeCommit {{ oid abbreviatedOid }} afterCommit {{ oid abbreviatedOid }} }}
... on LabeledEvent {{ id actor {{ {ACTOR} }} createdAt label {{ name color }} }}
... on UnlabeledEvent {{ id actor {{ {ACTOR} }} createdAt label {{ name color }} }}
... on ReviewRequestedEvent {{ id actor {{ {ACTOR} }} createdAt requestedReviewer {{
     __typename ... on User {{ login }} ... on Team {{ slug }} ... on Bot {{ login }} }} }}
... on CrossReferencedEvent {{ id actor {{ {ACTOR} }} createdAt willCloseTarget source {{
     __typename ... on Issue {{ number title url repository {{ nameWithOwner }} }}
     ... on PullRequest {{ number title url repository {{ nameWithOwner }} }} }} }}
... on MergedEvent {{ id actor {{ {ACTOR} }} createdAt }}
... on ClosedEvent {{ id actor {{ {ACTOR} }} createdAt }}
... on ReopenedEvent {{ id actor {{ {ACTOR} }} createdAt }}
... on ReadyForReviewEvent {{ id actor {{ {ACTOR} }} createdAt }}
... on ConvertToDraftEvent {{ id actor {{ {ACTOR} }} createdAt }}
... on AssignedEvent {{ id actor {{ {ACTOR} }} createdAt assignee {{ ... on User {{ login }} }} }}
... on RenamedTitleEvent {{ id actor {{ {ACTOR} }} createdAt previousTitle currentTitle }}
... on ReviewDismissedEvent {{ id actor {{ {ACTOR} }} createdAt }}
"""

_PR_ONLY = ("PullRequestReview", "PullRequestCommit", "HeadRefForcePushedEvent", "ReviewRequestedEvent",
            "MergedEvent", "ReadyForReviewEvent", "ConvertToDraftEvent", "ReviewDismissedEvent")


def _issue_fragments():
    """Drop PR-only fragments (each starts a line with '... on X') — GraphQL rejects them on issues."""
    out, skip = [], False
    for line in TIMELINE_FRAGMENTS.split("\n"):
        if line.startswith("... on "):
            skip = line.split()[2] in _PR_ONLY
        if not skip:
            out.append(line)
    return "\n".join(out)


ISSUE_TIMELINE_FRAGMENTS = _issue_fragments()

PR_TL_TYPES = ("ISSUE_COMMENT PULL_REQUEST_REVIEW PULL_REQUEST_COMMIT HEAD_REF_FORCE_PUSHED_EVENT "
               "LABELED_EVENT UNLABELED_EVENT REVIEW_REQUESTED_EVENT CROSS_REFERENCED_EVENT MERGED_EVENT "
               "CLOSED_EVENT REOPENED_EVENT READY_FOR_REVIEW_EVENT CONVERT_TO_DRAFT_EVENT ASSIGNED_EVENT "
               "RENAMED_TITLE_EVENT REVIEW_DISMISSED_EVENT").split()
ISSUE_TL_TYPES = ("ISSUE_COMMENT LABELED_EVENT UNLABELED_EVENT CROSS_REFERENCED_EVENT CLOSED_EVENT "
                  "REOPENED_EVENT ASSIGNED_EVENT RENAMED_TITLE_EVENT").split()

THREAD_FIELDS = f"""
id isResolved isOutdated path line originalLine startLine diffSide
comments(first: 100) {{ nodes {{ id author {{ {ACTOR} }} bodyHTML createdAt url diffHunk
  pullRequestReview {{ id }} }} }}
"""


def _pr_detail_query():
    return f"""
query($owner: String!, $name: String!, $number: Int!, $tlAfter: String, $thAfter: String,
      $wantMain: Boolean!, $wantTl: Boolean!, $wantTh: Boolean!) {{
  rateLimit {{ remaining }}
  repository(owner: $owner, name: $name) {{
    pullRequest(number: $number) {{
      ... @include(if: $wantMain) {{ {PR_LIST_FIELDS} bodyHTML }}
      timelineItems(first: 100, after: $tlAfter, itemTypes: [{",".join(PR_TL_TYPES)}]) @include(if: $wantTl) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{ {TIMELINE_FRAGMENTS} }}
      }}
      reviewThreads(first: 50, after: $thAfter) @include(if: $wantTh) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{ {THREAD_FIELDS} }}
      }}
    }}
  }}
}}"""


def _issue_detail_query():
    return f"""
query($owner: String!, $name: String!, $number: Int!, $tlAfter: String, $wantMain: Boolean!) {{
  rateLimit {{ remaining }}
  repository(owner: $owner, name: $name) {{
    issue(number: $number) {{
      ... @include(if: $wantMain) {{ {ISSUE_LIST_FIELDS} bodyHTML }}
      timelineItems(first: 100, after: $tlAfter, itemTypes: [{",".join(ISSUE_TL_TYPES)}]) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{ {ISSUE_TIMELINE_FRAGMENTS} }}
      }}
    }}
  }}
}}"""


PR_DETAIL_Q = _pr_detail_query()
ISSUE_DETAIL_Q = _issue_detail_query()

LIST_Q = """
query($owner: String!, $name: String!, $after: String, $states: [%(S)s!]) {
  rateLimit { remaining }
  repository(owner: $owner, name: $name) {
    conn: %(conn)s(first: %(n)d, after: $after, states: $states, orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes { %(fields)s }
    }
  }
}"""
PR_LIST_Q = LIST_Q % {"S": "PullRequestState", "conn": "pullRequests", "fields": PR_LIST_FIELDS, "n": 25}
ISSUE_LIST_Q = LIST_Q % {"S": "IssueState", "conn": "issues", "fields": ISSUE_LIST_FIELDS, "n": 100}

TAG_QUERIES = {
    "review_requested": "is:open review-requested:@me",
    "review_requested_direct": "is:open user-review-requested:@me",
    "author": "is:open author:@me",
    "assignee": "is:open assignee:@me",
    "mentioned": "is:open mentions:@me",
}

# Bump when the stored detail format changes; cached details get re-fetched in the background.
DETAIL_VERSION = "2"

# ---------------------------------------------------------------- helpers


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hidden_patterns(cfg: Config) -> list[str]:
    extra = [r["pattern"] for r in db.conn().execute("SELECT pattern FROM hidden_authors")]
    return list(cfg.hidden_authors) + extra


def _actor(a):
    if not a:
        return "ghost"
    return author_key(a.get("login"), a.get("__typename"))


def _reviewer(r):
    if not r:
        return None
    if r.get("__typename") == "Team":
        return "@" + r["slug"]
    return author_key(r.get("login"), r.get("__typename"))


def item_row_from_node(repo, kind, n):
    row = {
        "repo": repo, "number": n["number"], "kind": kind,
        "title": n["title"], "state": n["state"], "url": n["url"],
        "author": _actor(n.get("author")),
        "created_at": n["createdAt"], "updated_at": n["updatedAt"], "closed_at": n.get("closedAt"),
        "labels": json.dumps(n["labels"]["nodes"]),
        "assignees": json.dumps([a["login"] for a in n["assignees"]["nodes"]]),
        "comments_count": n["comments"]["totalCount"],
    }
    if kind == "pr":
        commits = n["commits"]["nodes"]
        rollup = commits[0]["commit"]["statusCheckRollup"] if commits else None
        row.update({
            "is_draft": int(n["isDraft"]),
            "review_requests": json.dumps([x for x in (_reviewer(rr["requestedReviewer"]) for rr in n["reviewRequests"]["nodes"]) if x]),
            "review_decision": n.get("reviewDecision"),
            "head_sha": n["headRefOid"], "base_ref": n["baseRefName"], "head_ref": n["headRefName"],
            "additions": n["additions"], "deletions": n["deletions"], "changed_files": n["changedFiles"],
            "ci_state": rollup["state"] if rollup else None,
        })
    return row


def upsert_item(row):
    cols = list(row.keys())
    sets = ",".join(f"{c}=excluded.{c}" for c in cols if c not in ("repo", "number"))
    db.conn().execute(
        f"INSERT INTO items({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
        f"ON CONFLICT(repo, number) DO UPDATE SET {sets}",
        [row[c] for c in cols],
    )


NOISE_TYPES = {"labeled", "unlabeled", "review_requested", "cross_ref", "assigned", "renamed",
               "force_push", "ready", "draft", "dismissed"}


def normalize_timeline(nodes):
    out = []
    for n in nodes:
        t = n.get("__typename")
        if not t or "id" not in n:
            continue
        e = {"id": n["id"]}
        if t == "IssueComment":
            e.update(type="comment", author=_actor(n["author"]), at=n["createdAt"], body=n["bodyHTML"], url=n["url"])
        elif t == "PullRequestReview":
            e.update(type="review", author=_actor(n["author"]), at=n.get("submittedAt") or n["createdAt"],
                     body=n["bodyHTML"], url=n["url"], state=n["state"],
                     commit=(n.get("commit") or {}).get("oid"))
        elif t == "PullRequestCommit":
            c = n["commit"]
            who = (c["author"].get("user") or {}).get("login") or c["author"].get("name") or "?"
            e.update(type="commit", author=who, at=c["committedDate"], sha=c["oid"], short=c["abbreviatedOid"],
                     text=c["messageHeadline"])
        else:
            e.update(author=_actor(n.get("actor")), at=n["createdAt"])
            if t == "HeadRefForcePushedEvent":
                b, a = n.get("beforeCommit") or {}, n.get("afterCommit") or {}
                e.update(type="force_push", text=f"force-pushed {b.get('abbreviatedOid', '?')} → {a.get('abbreviatedOid', '?')}",
                         sha=a.get("oid"), before=b.get("oid"))
            elif t == "LabeledEvent":
                e.update(type="labeled", text=f"added label {n['label']['name']}")
            elif t == "UnlabeledEvent":
                e.update(type="unlabeled", text=f"removed label {n['label']['name']}")
            elif t == "ReviewRequestedEvent":
                e.update(type="review_requested", text=f"requested review from {_reviewer(n.get('requestedReviewer')) or '?'}")
            elif t == "CrossReferencedEvent":
                s = n.get("source") or {}
                ref = f"{s.get('repository', {}).get('nameWithOwner', '')}#{s.get('number', '')}"
                e.update(type="cross_ref", text=f"referenced from {ref} {s.get('title', '')}", url=s.get("url"))
            elif t == "MergedEvent":
                e.update(type="merged", text="merged")
            elif t == "ClosedEvent":
                e.update(type="closed", text="closed")
            elif t == "ReopenedEvent":
                e.update(type="reopened", text="reopened")
            elif t == "ReadyForReviewEvent":
                e.update(type="ready", text="marked ready for review")
            elif t == "ConvertToDraftEvent":
                e.update(type="draft", text="converted to draft")
            elif t == "AssignedEvent":
                e.update(type="assigned", text=f"assigned {(n.get('assignee') or {}).get('login', '?')}")
            elif t == "RenamedTitleEvent":
                e.update(type="renamed", text=f"renamed from “{n['previousTitle']}”")
            elif t == "ReviewDismissedEvent":
                e.update(type="dismissed", text="dismissed a review")
            else:
                continue
        out.append(e)
    return out


def normalize_threads(nodes):
    out = []
    for t in nodes:
        comments = [{
            "id": c["id"], "author": _actor(c["author"]), "at": c["createdAt"], "body": c["bodyHTML"],
            "url": c["url"], "review_id": (c.get("pullRequestReview") or {}).get("id"),
        } for c in t["comments"]["nodes"]]
        if not comments:
            continue
        first = t["comments"]["nodes"][0]
        out.append({
            "id": t["id"], "resolved": t["isResolved"], "outdated": t["isOutdated"], "path": t["path"],
            "line": t["line"], "original_line": t["originalLine"], "start_line": t["startLine"],
            "side": t["diffSide"], "diff_hunk": first.get("diffHunk"), "comments": comments,
        })
    return out


TAG_RE = re.compile(r"<[^>]+>")


def plain_text(html):
    return unescape(TAG_RE.sub("", html or "")).strip()


def is_command_comment(e, cfg: Config):
    if e.get("type") != "comment" or not cfg.hidden_comment_patterns:
        return False
    text = plain_text(e.get("body"))
    return any(re.match(p, text, re.I) for p in cfg.hidden_comment_patterns)


def compute_activity_at(item_created, timeline, threads, patterns, cfg: Config):
    latest = item_created
    for e in timeline:
        if e["type"] in NOISE_TYPES or matches_any(e["author"], patterns) or is_command_comment(e, cfg):
            continue
        latest = max(latest, e["at"])
    for t in threads:
        for c in t["comments"]:
            if not matches_any(c["author"], patterns):
                latest = max(latest, c["at"])
    return latest


def recompute_all_activity(cfg: Config):
    """Re-derive activity_at after the hidden-author list changes."""
    pats = hidden_patterns(cfg)
    c = db.conn()
    for r in c.execute("SELECT i.repo, i.number, i.created_at, d.timeline, d.threads FROM items i "
                       "JOIN details d USING(repo, number)").fetchall():
        a = compute_activity_at(r["created_at"], db.jl(r["timeline"]), db.jl(r["threads"]), pats, cfg)
        c.execute("UPDATE items SET activity_at=? WHERE repo=? AND number=?", (a, r["repo"], r["number"]))
    c.commit()


# ---------------------------------------------------------------- syncer


class Syncer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.gh = GitHub()
        self.status = {"state": "idle", "last_sync": None, "error": None, "pending": 0, "done": 0}
        self._wake = asyncio.Event()
        self._item_locks: dict[tuple, asyncio.Lock] = {}

    # ---- list sync

    async def sync_lists(self, repo):
        owner, name = repo.split("/")
        since = db.get_meta(f"list_since:{repo}")
        started = now_iso()
        for kind, q in (("pr", PR_LIST_Q), ("issue", ISSUE_LIST_Q)):
            after = None
            # Initial sync: all open items. Incremental: everything (any state) updated since last run.
            states = ["OPEN"] if since is None else None
            while True:
                d = await self.gh.graphql(q, owner=owner, name=name, after=after, states=states)
                conn = d["repository"]["conn"]
                stop = False
                for n in conn["nodes"]:
                    if since and n["updatedAt"] < since:
                        stop = True
                        break
                    upsert_item(item_row_from_node(repo, kind, n))
                db.conn().commit()
                if stop or not conn["pageInfo"]["hasNextPage"]:
                    break
                after = conn["pageInfo"]["endCursor"]
        # Small overlap to avoid missing items updated during the sync.
        overlap = (datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ") - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        db.set_meta(f"list_since:{repo}", overlap)

    async def sync_tags(self):
        parts = []
        keys = []
        for ri, repo in enumerate(self.cfg.repos):
            for tag, q in TAG_QUERIES.items():
                alias = f"r{ri}_{tag}"
                keys.append((alias, repo, tag))
                parts.append(f'{alias}: search(query: {json.dumps(f"repo:{repo} {q}")}, type: ISSUE, first: 100) '
                             '{ nodes { ... on PullRequest { number } ... on Issue { number } } }')
        d = await self.gh.graphql("query { rateLimit { remaining } " + " ".join(parts) + " }")
        c = db.conn()
        for repo in self.cfg.repos:
            c.execute("DELETE FROM tags WHERE repo=? AND number IN (SELECT number FROM items WHERE repo=? AND state='OPEN')",
                      (repo, repo))
        for alias, repo, tag in keys:
            for n in d[alias]["nodes"]:
                if "number" in n:
                    c.execute("INSERT OR IGNORE INTO tags VALUES(?,?,?)", (repo, n["number"], tag))
        c.commit()

    # ---- detail sync

    def _lock(self, repo, number):
        return self._item_locks.setdefault((repo, number), asyncio.Lock())

    async def sync_detail(self, repo, number, kind=None):
        async with self._lock(repo, number):
            owner, name = repo.split("/")
            if kind is None:
                r = db.conn().execute("SELECT kind FROM items WHERE repo=? AND number=?", (repo, number)).fetchone()
                kind = r["kind"] if r else await self._detect_kind(owner, name, number)
            if kind == "pr":
                await self._sync_pr(repo, owner, name, number)
            else:
                await self._sync_issue(repo, owner, name, number)

    async def _detect_kind(self, owner, name, number):
        d = await self.gh.graphql("query($o:String!,$n:String!,$num:Int!){repository(owner:$o,name:$n)"
                                  "{issueOrPullRequest(number:$num){__typename}}}", o=owner, n=name, num=number)
        t = (d["repository"]["issueOrPullRequest"] or {}).get("__typename")
        if not t:
            raise LookupError(f"{owner}/{name}#{number} not found")
        return "pr" if t == "PullRequest" else "issue"

    async def _sync_pr(self, repo, owner, name, number):
        v = dict(owner=owner, name=name, number=number)
        d = await self.gh.graphql(PR_DETAIL_Q, **v, tlAfter=None, thAfter=None, wantMain=True, wantTl=True, wantTh=True)
        pr = d["repository"]["pullRequest"]
        tl_nodes, th_nodes = pr["timelineItems"]["nodes"], pr["reviewThreads"]["nodes"]
        tl_pi, th_pi = pr["timelineItems"]["pageInfo"], pr["reviewThreads"]["pageInfo"]
        while tl_pi["hasNextPage"] or th_pi["hasNextPage"]:
            more = await self.gh.graphql(
                PR_DETAIL_Q, **v, wantMain=False,
                wantTl=tl_pi["hasNextPage"], tlAfter=tl_pi["endCursor"],
                wantTh=th_pi["hasNextPage"], thAfter=th_pi["endCursor"])
            p = more["repository"]["pullRequest"]
            if tl_pi["hasNextPage"]:
                tl_nodes += p["timelineItems"]["nodes"]
                tl_pi = p["timelineItems"]["pageInfo"]
            if th_pi["hasNextPage"]:
                th_nodes += p["reviewThreads"]["nodes"]
                th_pi = p["reviewThreads"]["pageInfo"]

        row = item_row_from_node(repo, "pr", pr)
        timeline, threads = normalize_timeline(tl_nodes), normalize_threads(th_nodes)

        c = db.conn()
        prev = c.execute("SELECT files, files_sha FROM details WHERE repo=? AND number=?", (repo, number)).fetchone()
        if prev and prev["files_sha"] == row["head_sha"]:
            files_json = prev["files"]
        else:
            raw = await self.gh.rest_paginated(f"/repos/{repo}/pulls/{number}/files")
            files_json = json.dumps([{
                "filename": f["filename"], "status": f["status"], "additions": f["additions"],
                "deletions": f["deletions"], "patch": f.get("patch"), "previous_filename": f.get("previous_filename"),
            } for f in raw])

        row["activity_at"] = compute_activity_at(row["created_at"], timeline, threads, hidden_patterns(self.cfg), self.cfg)
        row["detail_updated_at"] = row["updated_at"]
        upsert_item(row)
        c.execute("INSERT OR REPLACE INTO details VALUES(?,?,?,?,?,?,?)",
                  (repo, number, pr["bodyHTML"], json.dumps(timeline), json.dumps(threads), files_json, row["head_sha"]))
        c.commit()

    async def _sync_issue(self, repo, owner, name, number):
        v = dict(owner=owner, name=name, number=number)
        d = await self.gh.graphql(ISSUE_DETAIL_Q, **v, tlAfter=None, wantMain=True)
        iss = d["repository"]["issue"]
        nodes, pi = iss["timelineItems"]["nodes"], iss["timelineItems"]["pageInfo"]
        while pi["hasNextPage"]:
            more = await self.gh.graphql(ISSUE_DETAIL_Q, **v, tlAfter=pi["endCursor"], wantMain=False)
            t = more["repository"]["issue"]["timelineItems"]
            nodes += t["nodes"]
            pi = t["pageInfo"]
        row = item_row_from_node(repo, "issue", iss)
        timeline = normalize_timeline(nodes)
        row["activity_at"] = compute_activity_at(row["created_at"], timeline, [], hidden_patterns(self.cfg), self.cfg)
        row["detail_updated_at"] = row["updated_at"]
        upsert_item(row)
        c = db.conn()
        c.execute("INSERT OR REPLACE INTO details VALUES(?,?,?,?,?,?,?)",
                  (repo, number, iss["bodyHTML"], json.dumps(timeline), "[]", None, None))
        c.commit()

    # ---- blobs (for "since last review" interdiffs)

    async def fetch_blobs(self, repo, pairs):
        """pairs: [(sha, path)] -> {(sha, path): text or None}"""
        c = db.conn()
        out, missing = {}, []
        for sha, path in pairs:
            r = c.execute("SELECT text FROM blobs WHERE sha=? AND path=?", (sha, path)).fetchone()
            if r:
                out[(sha, path)] = r["text"]
            else:
                missing.append((sha, path))
        owner, name = repo.split("/")
        for i in range(0, len(missing), 40):
            chunk = missing[i:i + 40]
            fields = " ".join(
                f'b{j}: object(expression: {json.dumps(f"{sha}:{path}")}) {{ ... on Blob {{ text isBinary }} }}'
                for j, (sha, path) in enumerate(chunk))
            d = await self.gh.graphql(f'query {{ repository(owner: "{owner}", name: "{name}") {{ {fields} }} }}')
            for j, key in enumerate(chunk):
                b = d["repository"].get(f"b{j}")
                text = None if (b is None or b.get("isBinary")) else (b.get("text") or "")
                # None = missing/binary; store "" marker only for real (possibly empty) text.
                if text is not None:
                    c.execute("INSERT OR REPLACE INTO blobs VALUES(?,?,?)", (key[0], key[1], text))
                out[key] = text
        c.commit()
        return out

    # ---- scheduler

    def _needs_detail(self):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.cfg.issue_prefetch_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        qs = ",".join("?" * len(self.cfg.repos))
        rows = db.conn().execute(f"""
            SELECT i.repo, i.number, i.kind,
                   EXISTS(SELECT 1 FROM tags t WHERE t.repo=i.repo AND t.number=i.number) AS tagged
            FROM items i
            WHERE i.repo IN ({qs})
              AND (i.detail_updated_at IS NULL OR i.detail_updated_at != i.updated_at)
              AND (
                (i.state = 'OPEN' AND (i.kind = 'pr' OR i.updated_at >= ?
                     OR EXISTS(SELECT 1 FROM tags t WHERE t.repo=i.repo AND t.number=i.number)))
                OR (i.detail_updated_at IS NOT NULL)  -- anything we've cached before stays fresh
              )
            ORDER BY tagged DESC, i.updated_at DESC
        """, (*self.cfg.repos, cutoff)).fetchall()
        return [(r["repo"], r["number"], r["kind"]) for r in rows]

    def check_detail_version(self):
        if db.get_meta("detail_version") != DETAIL_VERSION:
            db.conn().execute("UPDATE items SET detail_updated_at = '' WHERE detail_updated_at IS NOT NULL")
            db.set_meta("detail_version", DETAIL_VERSION)

    async def sync_once(self):
        t0 = time.time()
        self.status.update(state="syncing lists", error=None)
        for repo in self.cfg.repos:
            await self.sync_lists(repo)
        await self.sync_tags()
        todo = self._needs_detail()
        self.status.update(state="syncing details", pending=len(todo), done=0)

        async def one(repo, number, kind):
            try:
                await self.sync_detail(repo, number, kind)
            except Exception as e:  # keep going; one broken item shouldn't stall the rest
                log.warning("detail sync failed for %s#%s: %s", repo, number, e)
            self.status["done"] += 1

        await asyncio.gather(*(one(*t) for t in todo))
        self.status.update(state="idle", last_sync=now_iso(), pending=0)
        log.info("sync done in %.1fs (%d details), rate remaining %s", time.time() - t0, len(todo), self.gh.rate_remaining)

    def wake(self):
        self._wake.set()

    async def run_forever(self):
        while True:
            try:
                await self.sync_once()
            except Exception as e:
                log.exception("sync failed")
                self.status.update(state="error", error=str(e))
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.cfg.sync_interval_seconds)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
