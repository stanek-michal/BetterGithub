import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db
from .config import load_config, matches_any
from .diff import expand_full, make_patch, parse_patch, split_rows
from .summarize import Summarizer, html_to_text, render_summary
from .sync import NOISE_TYPES, Syncer, hidden_patterns, is_command_comment, now_iso, plain_text, recompute_all_activity

log = logging.getLogger("bgh")
HERE = Path(__file__).parent
cfg = load_config()
syncer: Syncer | None = None
summarizer: Summarizer | None = None


@asynccontextmanager
async def lifespan(app):
    global syncer, summarizer
    db.init()
    syncer = Syncer(cfg)
    summarizer = Summarizer(cfg.summarizer)
    recompute_all_activity(cfg)  # hidden-author config may have changed since last run
    syncer.check_detail_version()
    if not db.get_meta("viewer"):
        d = await syncer.gh.graphql("query { viewer { login } }")
        db.set_meta("viewer", d["viewer"]["login"])
    task = asyncio.create_task(syncer.run_forever())
    yield
    task.cancel()
    await syncer.gh.close()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


def reltime(iso):
    if not iso:
        return ""
    dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    s = (datetime.now(timezone.utc) - dt).total_seconds()
    for unit, n in (("y", 31536000), ("mo", 2592000), ("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{int(s // n)}{unit}"
    return "now"


templates.env.filters["rel"] = reltime
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else []


def label_fg(hex_color):
    try:
        r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
        return "#000" if (r * 299 + g * 587 + b * 114) / 1000 > 140 else "#fff"
    except Exception:
        return "#000"


templates.env.filters["label_fg"] = label_fg


def author_hue(login):
    """Stable per-user hue so the same person always gets the same color."""
    import zlib
    return zlib.crc32((login or "").encode()) % 360


templates.env.filters["hue"] = author_hue
templates.env.filters["textlen"] = lambda html: len(html_to_text(html))

# ---------------------------------------------------------------- list


def item_matches(view, it, tags, paths_for):
    state = view.get("state", "open")
    if state == "open" and it["state"] != "OPEN":
        return False
    if state == "closed" and it["state"] == "OPEN":
        return False
    if "kind" in view and it["kind"] != view["kind"]:
        return False
    if "repos" in view and it["repo"] not in view["repos"]:
        return False
    if "involves" in view and not (set(view["involves"]) & tags):
        return False
    labels = {l["name"] for l in it["labels_l"]}
    if "labels_any" in view and not (labels & set(view["labels_any"])):
        return False
    if "labels_none" in view and (labels & set(view["labels_none"])):
        return False
    if "authors" in view and not matches_any(it["author"], view["authors"]):
        return False
    if "exclude_authors" in view and matches_any(it["author"], view["exclude_authors"]):
        return False
    if "draft" in view and it["kind"] == "pr" and bool(it["is_draft"]) != view["draft"]:
        return False
    if "paths_any" in view:
        if it["kind"] != "pr" or not any(matches_any(p, view["paths_any"]) for p in paths_for(it)):
            return False
    return True


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, v: int = 0):
    views = cfg.views or [{"name": "All"}]
    v = max(0, min(v, len(views) - 1))
    view = views[v]
    c = db.conn()
    qs = ",".join("?" * len(cfg.repos))
    rows = [dict(r) for r in c.execute(f"""
        SELECT i.*, s.seen_at FROM items i LEFT JOIN seen s USING(repo, number)
        WHERE i.repo IN ({qs}) ORDER BY COALESCE(i.activity_at, i.updated_at) DESC""", cfg.repos)]
    tags = {}
    for t in c.execute("SELECT repo, number, tag FROM tags"):
        tags.setdefault((t["repo"], t["number"]), set()).add(t["tag"])

    def paths_for(it):
        d = c.execute("SELECT files FROM details WHERE repo=? AND number=?", (it["repo"], it["number"])).fetchone()
        return [f["filename"] for f in db.jl(d["files"] if d else None)]

    items = []
    for it in rows:
        it["labels_l"] = db.jl(it["labels"])
        for l in it["labels_l"]:
            l["hidden"] = matches_any(l["name"], cfg.list_hidden_labels)
        it["tags"] = tags.get((it["repo"], it["number"]), set())
        if item_matches(view, it, it["tags"], paths_for):
            act = it["activity_at"] or it["updated_at"]
            it["unread"] = it["seen_at"] is None or act > it["seen_at"]
            it["rr"] = db.jl(it["review_requests"])
            items.append(it)
        if len(items) >= 500:
            break
    return templates.TemplateResponse(request, "list.html", {
        "views": views, "v": v, "items": items, "status": {**syncer.status, "wait": syncer.gh.wait_note()}, "multi_repo": len(cfg.repos) > 1,
    })


# ---------------------------------------------------------------- item page


def build_conversation(item, detail, patterns, viewer):
    """Returns (entries, counts). Each entry has 'rh' = reason hidden in reduced view (or None)."""
    timeline = db.jl(detail["timeline"])
    threads = db.jl(detail["threads"])
    counts = {"author": 0, "resolved": 0, "noise": 0}

    def author_hidden(a):
        return a != viewer and matches_any(a, patterns)

    for t in threads:
        for cm in t["comments"]:
            cm["hidden"] = author_hidden(cm["author"])
        if t["resolved"] or t["outdated"]:
            t["rh"] = "resolved"
        elif all(cm["hidden"] for cm in t["comments"]):
            t["rh"] = "author"
        else:
            t["rh"] = None

    by_review = {}
    placed = set()
    for t in threads:
        rid = t["comments"][0].get("review_id")
        if rid:
            by_review.setdefault(rid, []).append(t)

    entries = []
    for e in timeline:
        e["hidden_author"] = author_hidden(e["author"])
        if e["type"] == "review":
            e["threads"] = by_review.get(e["id"], [])
            placed.update(t["id"] for t in e["threads"])
            has_body = bool((e.get("body") or "").strip())
            if e["hidden_author"]:
                e["rh"] = "author"
            elif not has_body and e["state"] == "COMMENTED" and all(t["rh"] for t in e["threads"]):
                e["rh"] = "noise"  # empty shell whose threads are all hidden
            else:
                e["rh"] = None
        elif e["type"] in NOISE_TYPES:
            e["rh"] = "noise"
        elif e["type"] == "commit":
            e["rh"] = None
        elif e["hidden_author"]:
            e["rh"] = "author"
        else:
            e["rh"] = "noise" if is_command_comment(e, cfg) else None
        entries.append(e)

    # Threads whose review isn't in the timeline (rare): show standalone at their time.
    for t in threads:
        if t["id"] not in placed:
            entries.append({"type": "thread", "id": t["id"], "at": t["comments"][0]["at"], "thread": t,
                            "author": t["comments"][0]["author"], "rh": t["rh"]})
    entries.sort(key=lambda e: e["at"])

    # Group consecutive commits.
    grouped = []
    for e in entries:
        if e["type"] == "commit" and grouped and grouped[-1]["type"] == "commits":
            grouped[-1]["commits"].append(e)
            grouped[-1]["at"] = e["at"]
        elif e["type"] == "commit":
            grouped.append({"type": "commits", "commits": [e], "at": e["at"], "rh": None, "id": e["id"]})
        else:
            grouped.append(e)

    for e in grouped:
        if e["rh"]:
            counts[e["rh"]] += 1
        for t in e.get("threads", []):
            if t["rh"] and not e["rh"]:
                counts[t["rh"]] += 1
    return grouped, threads, counts


GENERATED_RE = re.compile(r"(\.lock$|lock\.json$|\.pb\.go$|_pb2\.py$|\.min\.js$|/vendor/|^vendor/|\.snap$|go\.sum$)")


RENDER_BUDGET = 6000  # diff lines rendered inline; later files are collapsed and fetched on expand


def index_threads(threads):
    by_loc = {}
    for t in threads:
        if not t["outdated"] and t["line"]:
            by_loc.setdefault((t["path"], t["side"], t["line"]), []).append(t)
    return by_loc


def build_file(f, idx, threads, by_loc, split, interdiff, lazy=False, full_text=None):
    size = (f.get("additions") or 0) + (f.get("deletions") or 0)
    out = {**f, "idx": idx, "hunks": [], "lines": size, "other_threads": [], "lazy": lazy,
           "thread_count": sum(1 for t in threads if t["path"] == f["filename"])}
    out["collapsed"] = lazy or bool(GENERATED_RE.search(f["filename"])) or size > 1500 or bool(f.get("unchanged"))
    if lazy:
        return out
    hunks = parse_patch(f.get("patch")) if f.get("patch") else []
    if full_text is not None:
        hunks = [expand_full(hunks, full_text)]
    placed_ids = set()

    def threads_at(side, no):
        ts = by_loc.get((f["filename"], side, no), []) if no is not None else []
        placed_ids.update(t["id"] for t in ts)
        return ts

    for h in hunks:
        h["rows"] = []
        if split:
            for left, right in split_rows(h["lines"]):
                ts = threads_at("RIGHT", right[1]) if right else []
                if left and left[0] == "-" and not interdiff:
                    ts = ts + threads_at("LEFT", left[1])
                h["rows"].append({"left": left, "right": right, "threads": ts})
        else:
            for kind, o, n, text in h["lines"]:
                ts = threads_at("RIGHT", n)
                if kind == "-" and not interdiff:
                    ts = ts + threads_at("LEFT", o)
                h["rows"].append({"kind": kind, "o": o, "n": n, "text": text, "threads": ts})
    out["hunks"] = hunks
    out["other_threads"] = [t for t in threads if t["path"] == f["filename"] and t["id"] not in placed_ids]
    return out


def build_files(files, threads, split, interdiff=False):
    by_loc = index_threads(threads)
    out, budget = [], RENDER_BUDGET
    for idx, f in enumerate(files):
        size = (f.get("additions") or 0) + (f.get("deletions") or 0)
        auto_collapsed = bool(GENERATED_RE.search(f["filename"])) or size > 1500 or bool(f.get("unchanged"))
        lazy = auto_collapsed or budget <= 0
        if not lazy:
            budget -= size
        out.append(build_file(f, idx, threads, by_loc, split, interdiff, lazy=lazy))
    return out


async def interdiff_files(repo, files, base_sha, head_sha):
    pairs = []
    for f in files:
        old_path = f.get("previous_filename") or f["filename"]
        pairs.append((base_sha, old_path))
        pairs.append((base_sha, f["filename"]))
        pairs.append((head_sha, f["filename"]))
    blobs = await syncer.fetch_blobs(repo, list(dict.fromkeys(pairs)))
    out = []
    for f in files:
        old = blobs.get((base_sha, f["filename"]))
        if old is None and f.get("previous_filename"):
            old = blobs.get((base_sha, f["previous_filename"]))
        new = blobs.get((head_sha, f["filename"]))
        patch = make_patch(old, new) if (old or "") != (new or "") else ""
        adds = sum(1 for l in patch.split("\n") if l.startswith("+"))
        dels = sum(1 for l in patch.split("\n") if l.startswith("-"))
        out.append({"filename": f["filename"], "status": "modified" if patch else "unchanged",
                    "additions": adds, "deletions": dels, "patch": patch, "previous_filename": None,
                    "unchanged": not patch})
    # Changed files first, unchanged at the end.
    out.sort(key=lambda f: f["unchanged"])
    return out


def review_baseline(item, timeline, viewer):
    """Most recent of: local mark, my last submitted review commit. -> (sha, at, source) or None."""
    c = db.conn()
    cands = []
    m = c.execute("SELECT head_sha, marked_at FROM marks WHERE repo=? AND number=?", (item["repo"], item["number"])).fetchone()
    if m:
        cands.append((m["marked_at"], m["head_sha"], "marked locally"))
    for e in timeline:
        if e["type"] == "review" and e["author"] == viewer and e.get("commit"):
            cands.append((e["at"], e["commit"], "your review"))
    if not cands:
        return None
    at, sha, src = max(cands)
    return {"sha": sha, "at": at, "source": src}


async def load_item(repo, number):
    c = db.conn()
    item = c.execute("SELECT * FROM items WHERE repo=? AND number=?", (repo, number)).fetchone()
    detail = c.execute("SELECT * FROM details WHERE repo=? AND number=?", (repo, number)).fetchone()
    if item is None or detail is None or item["detail_updated_at"] is None:
        try:
            await syncer.sync_detail(repo, number)
        except LookupError:
            raise HTTPException(404)
        item = c.execute("SELECT * FROM items WHERE repo=? AND number=?", (repo, number)).fetchone()
        detail = c.execute("SELECT * FROM details WHERE repo=? AND number=?", (repo, number)).fetchone()
    return dict(item), dict(detail)


@app.get("/{owner}/{name}/pull/{number}", response_class=HTMLResponse)
@app.get("/{owner}/{name}/issues/{number}", response_class=HTMLResponse)
async def item_page(request: Request, owner: str, name: str, number: int, tab: str = "conv", since: str = ""):
    repo = f"{owner}/{name}"
    item, detail = await load_item(repo, number)
    viewer = db.get_meta("viewer")
    patterns = hidden_patterns(cfg)
    entries, threads, counts = build_conversation(item, detail, patterns, viewer)
    split = request.cookies.get("split") == "1"
    full = request.cookies.get("full") == "1"

    ctx = {
        "it": item, "d": detail, "entries": entries, "counts": counts, "full": full, "split": split,
        "tab": tab, "viewer": viewer, "labels": db.jl(item["labels"]), "rr": db.jl(item["review_requests"]),
        "assignees": db.jl(item["assignees"]), "status": {**syncer.status, "wait": syncer.gh.wait_note()}, "patterns": patterns,
        "body_hidden": item["author"] != viewer and matches_any(item["author"], patterns),
        "sum_min": summarizer.min_chars,
    }
    if item["kind"] == "pr":
        files = db.jl(detail["files"])
        baseline = review_baseline(item, db.jl(detail["timeline"]), viewer)
        ctx["baseline"] = baseline
        ctx["since"] = None
        # since=1 -> last review baseline; since=<sha> -> an arbitrary earlier push/commit.
        since_sha = baseline["sha"] if (since == "1" and baseline) else since if re.fullmatch(r"[0-9a-f]{7,40}", since) else None
        if tab == "files" and since_sha and since_sha != item["head_sha"]:
            files = await interdiff_files(repo, files, since_sha, item["head_sha"])
            ctx["since"] = {"sha": since_sha, "source": baseline["source"] if since == "1" else "selected commit"}
        ctx["files"] = build_files(files, threads, split, interdiff=bool(ctx["since"])) if tab == "files" else None
        ctx["file_count"] = len(db.jl(detail["files"]))

    c = db.conn()
    c.execute("INSERT OR REPLACE INTO seen VALUES(?,?,?)", (repo, number, now_iso()))
    c.commit()
    return templates.TemplateResponse(request, "item.html", ctx)


async def _file_context(repo, number, since):
    """Shared setup for per-file endpoints: item, threads, (inter)diff file list."""
    item, detail = await load_item(repo, number)
    viewer = db.get_meta("viewer")
    patterns = hidden_patterns(cfg)
    _, threads, _ = build_conversation(item, detail, patterns, viewer)
    files = db.jl(detail["files"])
    interdiff = bool(re.fullmatch(r"[0-9a-f]{7,40}", since)) and since != item["head_sha"]
    if interdiff:
        files = await interdiff_files(repo, files, since, item["head_sha"])
    return item, threads, files, interdiff, viewer, patterns


def _render_body(f, split, viewer, patterns):
    return templates.get_template("_filebody.html").render(f=f, split=split, viewer=viewer, patterns=patterns,
                                                         sum_min=summarizer.min_chars)


@app.get("/frag/{owner}/{name}/{number}/file/{idx}", response_class=HTMLResponse)
async def file_fragment(request: Request, owner: str, name: str, number: int, idx: int, since: str = "", full: int = 0):
    repo = f"{owner}/{name}"
    item, threads, files, interdiff, viewer, patterns = await _file_context(repo, number, since)
    if not 0 <= idx < len(files):
        raise HTTPException(404)
    split = request.cookies.get("split") == "1"
    full_text = None
    if full and files[idx]["status"] != "removed":
        path = files[idx]["filename"]
        full_text = (await syncer.fetch_blobs(repo, [(item["head_sha"], path)])).get((item["head_sha"], path))
        if full_text is None:
            return HTMLResponse('<div class="muted pad">Full file unavailable (binary or too large).</div>')
    f = build_file(files[idx], idx, threads, index_threads(threads), split, interdiff, full_text=full_text)
    return HTMLResponse(_render_body(f, split, viewer, patterns))


@app.get("/frag/{owner}/{name}/{number}/files")
async def files_fragment(request: Request, owner: str, name: str, number: int, idx: str, since: str = ""):
    """Batch version: idx=1,5,9 -> {idx: html}. Used by search to load many files at once."""
    item, threads, files, interdiff, viewer, patterns = await _file_context(f"{owner}/{name}", number, since)
    split = request.cookies.get("split") == "1"
    by_loc = index_threads(threads)
    out = {}
    for i in (int(x) for x in idx.split(",") if x.strip().isdigit()):
        if 0 <= i < len(files):
            out[i] = _render_body(build_file(files[i], i, threads, by_loc, split, interdiff), split, viewer, patterns)
    return out


@app.get("/api/search/{owner}/{name}/{number}")
async def search_files(owner: str, name: str, number: int, q: str, since: str = ""):
    """Which files contain q (smartcase, like the client)? Searches patch text, file names and thread comments."""
    item, threads, files, interdiff, viewer, patterns = await _file_context(f"{owner}/{name}", number, since)
    flags = re.I if q == q.lower() else 0
    rx = re.compile(re.escape(q), flags)
    comments = {}
    for t in threads:
        comments.setdefault(t["path"], []).extend(plain_text(c["body"]) for c in t["comments"])
    hits = [i for i, f in enumerate(files)
            if rx.search(f["filename"]) or rx.search(f.get("patch") or "")
            or any(rx.search(c) for c in comments.get(f["filename"], []))]
    return {"files": hits}


# ---------------------------------------------------------------- actions


@app.post("/api/refresh/{owner}/{name}/{number}")
async def api_refresh(owner: str, name: str, number: int):
    await syncer.sync_detail(f"{owner}/{name}", number)
    return {"ok": True}


def find_comment(item, detail, cid):
    """-> (html, context lines) for the description, a timeline comment/review, or a thread comment."""
    what = "pull request" if item["kind"] == "pr" else "issue"
    ctx = [f"This is a comment in the {what} #{item['number']} \"{item['title']}\" on {item['repo']}."]
    if cid == "desc":
        ctx.append(f"It is the {what} description, written by its author {item['author']}.")
        return detail["body_html"], ctx
    for e in db.jl(detail["timeline"]):
        if e["id"] == cid:
            kind = "a review summary" if e["type"] == "review" else "a comment"
            ctx.append(f"It is {kind} by {e['author']}.")
            return e.get("body"), ctx
    for t in db.jl(detail["threads"]):
        for i, c in enumerate(t["comments"]):
            if c["id"] == cid:
                ctx.append(f"It is an inline code review comment by {c['author']} on {t['path']}"
                           f"{':' + str(t['line'] or t['original_line']) if (t['line'] or t['original_line']) else ''}.")
                if t.get("diff_hunk"):
                    ctx.append("Code it refers to (end of diff hunk):\n" + "\n".join(t["diff_hunk"].split("\n")[-8:]))
                if i:
                    earlier = "\n".join(f"{p['author']}: {html_to_text(p['body'])[:1500]}" for p in t["comments"][:i])
                    ctx.append("Earlier comments in this thread (context only, do not summarize):\n" + earlier)
                return c["body"], ctx
    return None, ctx


@app.post("/api/summarize/{owner}/{name}/{number}")
async def api_summarize(owner: str, name: str, number: int, cid: str):
    item, detail = await load_item(f"{owner}/{name}", number)
    html, ctx = find_comment(item, detail, cid)
    if html is None:
        raise HTTPException(404, "comment not found")
    try:
        text = await summarizer.summarize(html_to_text(html), "\n\n".join(ctx))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return {"html": render_summary(text), "model": summarizer.model}


@app.post("/api/mark/{owner}/{name}/{number}")
async def api_mark(owner: str, name: str, number: int):
    repo = f"{owner}/{name}"
    r = db.conn().execute("SELECT head_sha FROM items WHERE repo=? AND number=?", (repo, number)).fetchone()
    if not r:
        raise HTTPException(404)
    db.conn().execute("INSERT OR REPLACE INTO marks VALUES(?,?,?,?)", (repo, number, r["head_sha"], now_iso()))
    db.conn().commit()
    return {"ok": True, "sha": r["head_sha"]}


@app.post("/api/hide")
async def api_hide(author: str, unhide: int = 0):
    c = db.conn()
    if unhide:
        c.execute("DELETE FROM hidden_authors WHERE pattern=?", (author,))
    else:
        c.execute("INSERT OR IGNORE INTO hidden_authors VALUES(?)", (author,))
    c.commit()
    recompute_all_activity(cfg)
    return {"ok": True}


@app.post("/api/sync")
async def api_sync():
    syncer.wake()
    return {"ok": True}


@app.get("/api/status")
async def api_status():
    return JSONResponse({**syncer.status, "rate_remaining": syncer.gh.rate_remaining, "wait": syncer.gh.wait_note()})


@app.get("/go")
async def go(ref: str):
    """Jump to 'owner/repo#123', '#123' / '123' (first repo), or a github.com URL."""
    ref = ref.strip()
    m = re.search(r"github\.com/([^/]+/[^/]+)/(?:pull|issues)/(\d+)", ref) or re.match(r"^([\w.-]+/[\w.-]+)#(\d+)$", ref)
    if m:
        repo, num = m.group(1), int(m.group(2))
    elif re.match(r"^#?\d+$", ref):
        repo, num = cfg.repos[0], int(ref.lstrip("#"))
    else:
        raise HTTPException(400, "can't parse ref")
    r = db.conn().execute("SELECT kind FROM items WHERE repo=? AND number=?", (repo, num)).fetchone()
    kind = r["kind"] if r else await syncer._detect_kind(*repo.split("/"), num)
    return RedirectResponse(f"/{repo}/{'pull' if kind == 'pr' else 'issues'}/{num}", status_code=303)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    uvicorn.run("bgh.app:app", host="127.0.0.1", port=cfg.port, log_level="warning")
