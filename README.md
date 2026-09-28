# bgh — BetterGithub

A fast, local, read-only GitHub viewer for the PRs and issues you care about.
It syncs repos into SQLite in the background, so pages load from local disk in about 5–50ms.

## Setup
Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), and the [GitHub CLI](https://cli.github.com/) (`gh`).

```
gh auth login                        # bgh uses `gh auth token` for GitHub API access
cp config.example.toml config.toml   # then set `repos` to the repos you want to sync
export GEMINI_API_KEY=...            # optional: enables ✦ comment summaries
uv run bgh                           # http://127.0.0.1:7777
```

Edit `config.toml` and set `repos` to the repos you want to sync, e.g. `repos = ["python/cpython"]`.
bgh won't start until you do. Everything else has sensible defaults.

Configuration is in `config.toml`: repos, hidden authors, comment patterns treated as noise,
list labels to hide, and views. Restart after editing it. `config.toml` and the local database
(`bgh.db`) are gitignored, so your repos and synced data stay on your machine.

## Reduced vs full view (`v`)
The reduced view hides:
- comments/reviews by `hidden_authors` (globs; bots match as `name[bot]`), plus authors hidden via the ⊘ button next to a name
- resolved or outdated review threads
- timeline noise (labels, review requests, cross-refs, assignments, force-pushes, …) and CI command comments (`hidden_comment_patterns`, e.g. `/test`)

A description written by a hidden author is collapsed rather than removed. Unread status and list
sorting use the last *non-hidden* activity, so bot comments don't bump items.

## Diffs
- `f` (or the "full file" button) shows the whole file with the diff highlighted in place. Press it again for hunks only.
  In full view, `]`/`[` jump between change blocks.
- `s` split/unified, `j/k` files, `]/[` hunks, `x` collapse or expand. Big PRs render the first
  ~6k lines; later files load when you expand them.
- Review threads appear inline at their line. Threads outside the diff context show at the top of the file.
- **Since last review** (`i`): diffs the PR's files between your baseline and the head. The baseline is
  the later of your last submitted GitHub review and a local `m` mark. This works across force-pushes
  because both snapshots are fetched and diffed locally. If the branch was rebased, upstream edits to
  the same files show up too.
- The Δ next to any commit or force-push in the conversation shows the changes since that point.

## LLM summaries
Comments longer than `min_chars` get a **✦ summary** button in their header (or press `S` on the
selected entry). The summary is shown in a dashed purple box labeled **AI SUMMARY**, and the
original is hidden until you click **↩ original**. It uses `gemini-3.8-flash` through Gemini's
OpenAI-compatible API. The key comes from `[summarizer] api_key` in `config.toml`, or else `$GEMINI_API_KEY`.
Summaries are cached in the DB, so reopening one costs nothing. For review-thread replies, the code
and earlier replies are sent as context.

## Keyboard reference
Press `?` on any page to show these keys in an overlay.

**Everywhere**
| key | action |
|---|---|
| `gg` / `G` | jump to top / bottom of the page |
| `:` | jump box: type `123`, `#123`, `owner/repo#123`, or paste a github.com URL, then Enter |
| `?` | show/hide key help |
| `Esc` | close help / leave a text box |

**List page**
| key | action |
|---|---|
| `j` / `k` | move down / up |
| `Enter` or `o` | open selected item |
| `1`–`9` | switch view (Mine, Review requested, All PRs, …) |
| `/` | filter by text (title, number, author, label). `Enter` selects the first match; press `Enter` again to open it |
| `r` | sync now |

**PR / issue page**
| key | action |
|---|---|
| `v` | toggle reduced / full view (remembered) |
| `c` | conversation tab |
| `d` | files / diff tab (PRs) |
| `u` | back to list |
| `o` | open on github.com |
| `R` | refresh this item from GitHub now |
| `m` | mark current head as reviewed (baseline for "since last review") |

**Search (PR / issue pages, vim-style)**
| key | action |
|---|---|
| `/` | search; type the query, then `Enter` jumps to the first match at or after where you are |
| `n` / `N` | next / previous match (wraps around; the counter shows `3/17`) |
| `Esc` | clear the search and its highlights |

Smartcase: an all-lowercase query ignores case, and any capital letter makes it case-sensitive. On the files
tab, search first expands and loads every file so nothing is missed. In reduced view, hidden
comments are skipped.

**Conversation tab**
| key | action |
|---|---|
| `j` / `k` | next / previous comment or event |
| `S` | LLM summary ⇄ original for the selected entry (same as its ✦ summary button) |

**Files tab**
| key | action |
|---|---|
| `j` / `k` | next / previous file |
| `]` / `[` | next / previous hunk (change block in full-file view) |
| `x` | collapse / expand current file |
| `f` | full file with the diff highlighted in place / back to hunks |
| `s` | split / unified (remembered) |
| `i` | toggle "since last review" |

j/k and ]/[ continue from whatever is on screen, so mouse scrolling and keys work together.
With the mouse: ⊘ next to a name hides that author, and Δ next to a commit or force-push
shows the changes since that point.

## License
MIT. See [LICENSE](LICENSE).
