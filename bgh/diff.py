"""Parse GitHub per-file patches and build row models for unified / split rendering."""
import difflib
import re

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def parse_patch(patch: str):
    """-> list of hunks: {header, lines: [(kind, old_no, new_no, text)]} kind in ' ', '-', '+'."""
    hunks = []
    cur = None
    old = new = 0
    for raw in (patch or "").split("\n"):
        m = HUNK_RE.match(raw)
        if m:
            old, new = int(m.group(1)), int(m.group(3))
            cur = {"header": raw, "lines": []}
            hunks.append(cur)
            continue
        if cur is None or raw.startswith("\\"):
            continue
        k, text = (raw[:1] or " "), raw[1:]
        if k == "+":
            cur["lines"].append(("+", None, new, text))
            new += 1
        elif k == "-":
            cur["lines"].append(("-", old, None, text))
            old += 1
        else:
            cur["lines"].append((" ", old, new, text))
            old += 1
            new += 1
    return hunks


def split_rows(lines):
    """Pair up deletions/additions for side-by-side view.
    -> list of (left, right) where each is (kind, no, text) or None."""
    rows = []
    i = 0
    while i < len(lines):
        k = lines[i][0]
        if k == " ":
            _, o, n, t = lines[i]
            rows.append(((" ", o, t), (" ", n, t)))
            i += 1
            continue
        dels, adds = [], []
        while i < len(lines) and lines[i][0] == "-":
            dels.append(lines[i]); i += 1
        while i < len(lines) and lines[i][0] == "+":
            adds.append(lines[i]); i += 1
        for j in range(max(len(dels), len(adds))):
            left = ("-", dels[j][1], dels[j][3]) if j < len(dels) else None
            right = ("+", adds[j][2], adds[j][3]) if j < len(adds) else None
            rows.append((left, right))
    return rows


def make_patch(old_text: str | None, new_text: str | None, context=3) -> str:
    a = (old_text or "").splitlines()
    b = (new_text or "").splitlines()
    out = difflib.unified_diff(a, b, lineterm="", n=context)
    return "\n".join(l for l in out if not l.startswith(("---", "+++")))


def expand_full(hunks, new_text: str):
    """Merge diff hunks into the complete new file: one hunk covering every line, with the
    changed regions exactly as in the patch and all unchanged lines in between as context."""
    new_lines = new_text.split("\n")
    if new_lines and new_lines[-1] == "":
        new_lines.pop()
    out = []
    new_pos, delta = 1, 0  # delta = old_no - new_no for unchanged lines at this point

    def context_until(stop):
        nonlocal new_pos
        while new_pos < stop and new_pos <= len(new_lines):
            out.append((" ", new_pos + delta, new_pos, new_lines[new_pos - 1]))
            new_pos += 1

    for h in hunks:
        m = HUNK_RE.match(h["header"])
        os_, ns = int(m.group(1)), int(m.group(3))
        oc = int(m.group(2)) if m.group(2) is not None else 1
        nc = int(m.group(4)) if m.group(4) is not None else 1
        # A zero count means the start number is the line *before* the hunk.
        new_first = ns if nc else ns + 1
        old_first = os_ if oc else os_ + 1
        context_until(new_first)
        out.extend(h["lines"])
        new_pos = new_first + nc
        delta = (old_first + oc) - new_pos
    context_until(len(new_lines) + 1)
    return {"header": f"full file · {len(new_lines)} lines", "lines": out}
