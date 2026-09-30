(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const post = (url) => fetch(url, { method: "POST" }).then((r) => r.json());
  const setCookie = (k, v) => (document.cookie = `${k}=${v}; path=/; max-age=31536000; samesite=lax`);
  const visible = (el) => el.offsetParent !== null;
  const item = $("#item");
  function toast(msg) {
    const t = Object.assign(document.createElement("div"), { className: "toast", textContent: msg });
    document.body.append(t);
    setTimeout(() => t.remove(), 3000);
  }

  // ---- selection helper (list rows, conversation entries, files, hunks)
  function mover(getEls, cls = "sel") {
    let i = -1;
    return (delta) => {
      const els = getEls().filter(visible);
      if (!els.length) return;
      let cur = els.findIndex((e) => e.classList.contains(cls));
      const onScreen = (e) => { const r = e.getBoundingClientRect(); return r.bottom > 90 && r.top < innerHeight; };
      // If the selection scrolled away (mouse/trackpad), continue from what's on screen instead.
      if (cur < 0 || !onScreen(els[cur])) {
        const first = els.findIndex((e) => e.getBoundingClientRect().top >= 90);
        cur = first < 0 ? els.length : first;
        i = delta > 0 ? cur - 1 : cur;
      } else i = cur;
      els.forEach((e) => e.classList.remove(cls));
      i = Math.max(0, Math.min(els.length - 1, i + delta));
      els[i].classList.add(cls);
      const top = els[i].getBoundingClientRect().top;
      window.scrollBy({ top: top - 100 });
      return els[i];
    };
  }

  // ---- help
  $("#helpbtn").onclick = (e) => { e.preventDefault(); $("#help").hidden = !$("#help").hidden; };

  // ---- hide/unhide author buttons
  const hidden = new Set();
  document.addEventListener("click", async (e) => {
    const b = e.target.closest(".hidebtn");
    if (!b) return;
    e.preventDefault();
    const a = b.dataset.author;
    const isHidden = b.closest(".hiddenauthor");
    if (!confirm(`${isHidden ? "Unhide" : "Hide"} ${a} in reduced view?` + (isHidden ? "\n(Only works for authors hidden from the UI; config.toml patterns must be edited there.)" : ""))) return;
    await post(`/api/hide?author=${encodeURIComponent(a)}${isHidden ? "&unhide=1" : ""}`);
    location.reload();
  });

  // ---- file collapse (bodies of big/late files are fetched on first expand)
  async function toggleFile(file) {
    const expanding = file.classList.toggle("collapsed") === false;
    const body = $(".fbody", file);
    if (expanding && body.dataset.lazy && !body.dataset.loaded) {
      body.dataset.loaded = "1";
      body.innerHTML = '<div class="muted pad">loading…</div>';
      body.innerHTML = await fetch(body.dataset.lazy).then((r) => r.text());
    }
  }
  // Full file view: swap the hunk view for the whole file (diff highlighted in place) and back.
  async function toggleFull_(file) {
    const btn = $(".fullbtn", file);
    if (!btn) return;
    const body = $(".fbody", file);
    file.classList.remove("collapsed");
    if (file.dataset.fullOn) {
      body.innerHTML = file._hunkHtml;
      delete file.dataset.fullOn;
      btn.lastChild.textContent = " full file";
    } else {
      if (body.dataset.lazy && !body.dataset.loaded) await toggleFile(file), file.classList.remove("collapsed");
      file._hunkHtml = body.innerHTML;
      body.innerHTML = '<div class="muted pad">loading full file…</div>';
      body.innerHTML = await fetch(btn.dataset.url).then((r) => r.text());
      file.dataset.fullOn = "1";
      btn.lastChild.textContent = " hunks only";
    }
    if (search.q) await runSearch(search.q); // re-highlight the swapped-in content
  }
  document.addEventListener("click", (e) => {
    const b = e.target.closest(".fullbtn");
    if (!b) return;
    e.preventDefault();
    toggleFull_(b.closest(".file"));
  });
  document.addEventListener("click", (e) => {
    const t = e.target.closest(".ftoggle");
    if (!t) return;
    e.preventDefault();
    toggleFile(t.closest(".file"));
  });
  // Clicking a collapsed file in the file list expands it.
  document.addEventListener("click", (e) => {
    const a = e.target.closest(".filelist a");
    if (!a) return;
    const f = $(a.getAttribute("href"));
    if (f && f.classList.contains("collapsed")) toggleFile(f);
  });

  // ---- list page
  const filter = $("#filter");
  if (filter) {
    filter.addEventListener("input", () => {
      const q = filter.value.toLowerCase().trim().split(/\s+/);
      $$("#items tr.row").forEach((r) => (r.style.display = q.every((w) => r.dataset.search.includes(w)) ? "" : "none"));
    });
    filter.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === "Escape") {
        e.preventDefault(); e.stopPropagation(); filter.blur();
        $$("#items tr.sel").forEach((r) => r.classList.remove("sel"));
        moveRow(1);
      }
    });
  }
  const moveRow = mover(() => $$("#items tr.row"));
  $$("#items tr.row").forEach((r) => r.addEventListener("click", (e) => { if (!e.target.closest("a")) location.href = r.dataset.href; }));

  // ---- item page
  const toggleFull = () => {
    const full = document.body.classList.toggle("reduced") === false;
    setCookie("full", full ? "1" : "0");
  };
  if ($("#togglefull")) $("#togglefull").onclick = (e) => { e.preventDefault(); toggleFull(); };
  const moveEntry = mover(() => $$("#conv .entry"));
  const moveFile = mover(() => $$("#files .file"));
  // Hunks; inside full-file views, each block of changed lines counts as a hunk.
  const isChange = (tr) => tr && (tr.matches("tr.a, tr.d") || !!tr.querySelector("td.ca, td.cd"));
  const moveHunk = mover(() => $$("#files .file:not(.collapsed)").flatMap((f) =>
    f.dataset.fullOn
      ? $$("tbody.hunk-b tr", f).filter((tr) => isChange(tr) && !isChange(tr.previousElementSibling))
      : $$("tbody.hunk-b", f)));
  const itemBase = item && `/${item.dataset.repo}/${location.pathname.includes("/pull/") ? "pull" : "issues"}/${item.dataset.number}`;

  function currentFile() {
    return $("#files .file.sel") || $$("#files .file").find((f) => f.getBoundingClientRect().bottom > 120);
  }

  // Poll sync status while the initial sync runs so the list fills in.
  const st = $("#syncstatus");
  if (st && !st.textContent.trim().startsWith("idle")) {
    const iv = setInterval(async () => {
      const s = await fetch("/api/status").then((r) => r.json());
      st.textContent = s.state + (s.pending ? ` ${s.done}/${s.pending}` : "") + (s.wait ? ` · ${s.wait}` : "") + (s.error ? ` · ${s.error}` : "");
      if (s.state === "idle") { clearInterval(iv); if (!item && !$("#items tr.row")) location.reload(); }
    }, 2000);
  }

  // ---- LLM summaries: "✦ summary" button per comment, S on the selected entry
  async function setSummary(btn, on) {
    const unit = btn.closest(".cmt, .entry");
    const body = unit.querySelector(":scope > .body");
    let box = unit.querySelector(":scope > .llmsum");
    if (on && !box) {
      if (btn.dataset.busy) return;
      btn.dataset.busy = "1";
      btn.textContent = "✦ summarizing…";
      try {
        const r = await fetch(`/api/summarize/${item.dataset.repo}/${item.dataset.number}?cid=${encodeURIComponent(btn.dataset.cid)}`, { method: "POST" });
        const j = await r.json();
        if (!r.ok) throw new Error(j.error || j.detail || r.status);
        box = document.createElement("div");
        box.className = "llmsum";
        box.innerHTML = `<div class="llmhead">✦ AI SUMMARY <span class="llmmodel">${j.model} · not the author's words</span>` +
          `<a href="#" class="llmorig">show original ↩</a></div><div class="llmbody">${j.html}</div>`;
        $(".llmorig", box).onclick = (e) => { e.preventDefault(); setSummary(btn, false); };
        body.before(box);
      } catch (err) {
        toast(`Summary failed: ${err.message}`);
        btn.textContent = "✦ summary";
        return;
      } finally {
        delete btn.dataset.busy;
      }
    }
    if (box) box.hidden = !on;
    body.hidden = on;
    unit.classList.toggle("showing-summary", on);
    btn.textContent = on ? "↩ original" : "✦ summary";
  }
  document.addEventListener("click", (e) => {
    const b = e.target.closest(".sumbtn");
    if (!b) return;
    e.preventDefault();
    setSummary(b, !b.closest(".cmt, .entry").classList.contains("showing-summary"));
  });
  function toggleEntrySummaries() {
    const entry = $("#conv .entry.sel") || $$("#conv .entry").filter(visible).find((e) => e.getBoundingClientRect().bottom > 100);
    if (!entry) return;
    const btns = $$(".sumbtn", entry).filter(visible);
    if (!btns.length) return toast("Nothing long enough to summarize here.");
    const on = !btns[0].closest(".cmt, .entry").classList.contains("showing-summary");
    btns.forEach((b) => setSummary(b, on));
  }

  // ---- vim-style search on item pages: / query Enter, n / N, Esc clears
  const search = { q: "", marks: [], i: -1 };
  let sbar;
  function searchBar() {
    if (sbar) return sbar;
    sbar = Object.assign(document.createElement("div"), { id: "searchbar" });
    sbar.innerHTML = '<span>/</span><input id="searchinput" autocomplete="off" spellcheck="false"><span id="searchinfo" class="muted"></span>';
    document.body.append(sbar);
    const inp = $("#searchinput", sbar);
    inp.addEventListener("keydown", async (e) => {
      e.stopPropagation();
      if (e.key === "Enter") { e.preventDefault(); inp.blur(); await runSearch(inp.value); jump(0, true); }
      else if (e.key === "Escape") { e.preventDefault(); inp.blur(); clearSearch(); sbar.hidden = true; }
    });
    return sbar;
  }
  function clearSearch() {
    for (const m of search.marks) m.replaceWith(document.createTextNode(m.textContent));
    search.marks = []; search.i = -1;
    ($("#conv") || $("#files") || document.body).normalize();
    if (sbar) $("#searchinfo", sbar).textContent = "";
  }
  // Ask the server which files match, then batch-load and expand only those.
  async function loadMatchingFiles(q) {
    const sinceSha = item.dataset.since;
    const qs = (o) => new URLSearchParams(Object.fromEntries(Object.entries(o).filter(([, v]) => v))).toString();
    const base = `/${item.dataset.repo}/${item.dataset.number}`;
    const { files: hits } = await fetch(`/api/search${base}?${qs({ q, since: sinceSha })}`).then((r) => r.json());
    const info = $("#searchinfo", searchBar());
    const fileEls = $$("#files .file");
    const toLoad = hits.filter((i) => { const b = $(".fbody", fileEls[i]); return b.dataset.lazy && !b.dataset.loaded; });
    let done = 0;
    const chunks = [];
    for (let i = 0; i < toLoad.length; i += 40) chunks.push(toLoad.slice(i, i + 40));
    const worker = async () => {
      for (let c; (c = chunks.shift()); ) {
        const html = await fetch(`/frag${base}/files?${qs({ idx: c.join(","), since: sinceSha })}`).then((r) => r.json());
        for (const [i, h] of Object.entries(html)) {
          const b = $(".fbody", fileEls[i]);
          b.dataset.loaded = "1";
          b.innerHTML = h;
        }
        done += c.length;
        info.textContent = `loading ${done}/${toLoad.length} matching files…`;
      }
    };
    await Promise.all(Array.from({ length: 4 }, worker));
    hits.forEach((i) => fileEls[i].classList.remove("collapsed"));
  }
  async function runSearch(q) {
    clearSearch();
    search.q = q;
    if (!q) return;
    if ($("#files")) await loadMatchingFiles(q);
    const root = $("#conv") || $("#files");
    const flags = q === q.toLowerCase() ? "gi" : "g"; // smartcase
    const re = new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), flags);
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => (n.parentElement.closest(".rh, script, style") && document.body.classList.contains("reduced")) || n.parentElement.closest(".filelist")
        || !re.test(n.data) ? NodeFilter.FILTER_REJECT : (re.lastIndex = 0, NodeFilter.FILTER_ACCEPT),
    });
    const nodes = [];
    for (let n; (n = walker.nextNode()); ) nodes.push(n);
    for (const n of nodes) {
      re.lastIndex = 0;
      const frag = document.createDocumentFragment();
      let last = 0, m;
      while ((m = re.exec(n.data))) {
        frag.append(n.data.slice(last, m.index));
        const mk = Object.assign(document.createElement("mark"), { className: "sm", textContent: m[0] });
        frag.append(mk);
        search.marks.push(mk);
        last = m.index + m[0].length;
        if (!m[0].length) re.lastIndex++;
      }
      frag.append(n.data.slice(last));
      n.replaceWith(frag);
    }
  }
  function jump(delta, fromScreen = false) {
    const info = $("#searchinfo", searchBar());
    sbar.hidden = false;
    const marks = search.marks.filter(visible);
    if (!marks.length) { info.textContent = search.q ? `Pattern not found: ${search.q}` : ""; return; }
    let i = marks.indexOf(search.marks[search.i]);
    if (fromScreen || i < 0) {
      // like vim: first match at/after the current screen position
      i = marks.findIndex((m) => m.getBoundingClientRect().top >= 90);
      if (i < 0) i = 0;
    } else {
      i = (i + delta + marks.length) % marks.length;
    }
    search.marks.forEach((m) => m.classList.remove("cur"));
    marks[i].classList.add("cur");
    search.i = search.marks.indexOf(marks[i]);
    marks[i].scrollIntoView({ block: "center" });
    info.textContent = `${i + 1}/${marks.length}`;
  }

  document.addEventListener("keydown", async (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const tag = document.activeElement && document.activeElement.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA") {
      if (e.key === "Escape") document.activeElement.blur();
      return;
    }
    const k = e.key;
    if (k === "?") { $("#help").hidden = !$("#help").hidden; return; }
    if (k === "Escape") { $("#help").hidden = true; if (item) { clearSearch(); search.q = ""; if (sbar) sbar.hidden = true; } return; }
    if (k === ":") { e.preventDefault(); $("#goinput").focus(); return; }
    // vim-style: gg = top, G = bottom
    if (k === "G") { window.scrollTo(0, document.body.scrollHeight); return; }
    if (k === "g") {
      if (Date.now() - (window._lastG || 0) < 600) { window._lastG = 0; window.scrollTo(0, 0); }
      else window._lastG = Date.now();
      return;
    }

    if (!item) {
      if (k === "j") moveRow(1);
      else if (k === "k") moveRow(-1);
      else if (k === "Enter" || k === "o") { const r = $("#items tr.sel"); if (r) location.href = r.dataset.href; }
      else if (k === "/") { e.preventDefault(); filter.focus(); filter.select(); }
      else if (k === "r") { await post("/api/sync"); st.textContent = "sync requested…"; setTimeout(() => location.reload(), 4000); }
      else if (/^[1-9]$/.test(k)) location.href = `/?v=${+k - 1}`;
      return;
    }

    const onFiles = item.dataset.tab === "files";
    if (k === "/") { e.preventDefault(); const b = searchBar(); b.hidden = false; const inp = $("#searchinput", b); inp.value = search.q; inp.focus(); inp.select(); }
    else if (k === "n" || k === "N") {
      if (search.q && !search.marks.some((m) => m.isConnected)) await runSearch(search.q); // DOM changed (e.g. full-file toggle)
      jump(k === "n" ? 1 : -1);
    }
    else if (k === "S") toggleEntrySummaries();
    else if (k === "v") toggleFull();
    else if (k === "c") location.href = itemBase;
    else if (k === "d" && itemBase.includes("/pull/")) location.href = itemBase + "?tab=files";
    else if (k === "u") location.href = "/";
    else if (k === "o") window.open(item.dataset.url, "_blank");
    else if (k === "R") { $("#syncstatus").textContent = "refreshing…"; await post(`/api/refresh/${item.dataset.repo}/${item.dataset.number}`); location.reload(); }
    else if (k === "s") { setCookie("split", document.body.classList.contains("split") ? "0" : "1"); location.reload(); }
    else if (k === "i" && onFiles) {
      const u = new URL(location.href);
      if (u.searchParams.get("since")) u.searchParams.delete("since"); else u.searchParams.set("since", "1");
      location.href = u.toString();
    }
    else if (k === "m" && itemBase.includes("/pull/")) {
      const r = await post(`/api/mark/${item.dataset.repo}/${item.dataset.number}`);
      toast(`Marked ${r.sha.slice(0, 10)} as reviewed — "since last review" now diffs from here.`);
    }
    else if (k === "j") onFiles ? moveFile(1) : moveEntry(1);
    else if (k === "k") onFiles ? moveFile(-1) : moveEntry(-1);
    else if (k === "]" && onFiles) moveHunk(1);
    else if (k === "[" && onFiles) moveHunk(-1);
    else if (k === "x" && onFiles) { const f = currentFile(); if (f) toggleFile(f); }
    else if (k === "f" && onFiles) { const f = currentFile(); if (f) toggleFull_(f); }
  });
})();
