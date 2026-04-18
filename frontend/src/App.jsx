import { useState, useEffect, useRef } from "react";
import mermaid from "mermaid";
import { marked } from "marked";

const API = `http://${window.location.hostname}:8001`;

// ── marked config ─────────────────────────────────────────────────────────────
marked.setOptions({ breaks: false, gfm: true });

// ── Mermaid ───────────────────────────────────────────────────────────────────

function sanitizeMermaid(chart) {
  // Flatten newlines inside flowchart node labels [...]
  let out = chart.replace(/\[([^\]]*)\]/g, (_, inner) => `[${inner.replace(/\\n|\n/g, " ")}]`);
  // Flatten newlines inside sequence diagram note text (note over X: ...)
  out = out.replace(/(note\s+(?:over|left of|right of)\s+[^:]+:\s*)([^\n]+(?:\n(?!(?:note|end|loop|alt|else|opt|par|and|rect|activate|deactivate|\w+--?>>|\w+->))[^\n]+)*)/gi,
    (_, prefix, body) => prefix + body.replace(/\n/g, " "));
  return out;
}

let _mermaidReady = false;
function ensureMermaid() {
  if (!_mermaidReady) {
    mermaid.initialize({ startOnLoad: false, theme: "neutral", securityLevel: "loose" });
    _mermaidReady = true;
  }
}

function MermaidDiagram({ chart }) {
  const uid = useRef(`md-${Math.random().toString(36).slice(2)}`);
  const [svg, setSvg] = useState("");
  const [rawFallback, setRawFallback] = useState(false);

  useEffect(() => {
    if (!chart) return;
    ensureMermaid();
    setSvg(""); setRawFallback(false);
    const clean = sanitizeMermaid(chart);
    const tmp = document.createElement("div");
    tmp.style.position = "absolute"; tmp.style.visibility = "hidden";
    document.body.appendChild(tmp);
    mermaid.render(uid.current, clean, tmp)
      .then(({ svg: s }) => setSvg(s))
      .catch(() => setRawFallback(true))
      .finally(() => { try { document.body.removeChild(tmp); } catch {} });
  }, [chart]);

  if (!chart) return null;
  if (rawFallback) return (
    <pre className="text-xs text-stone-500 bg-stone-50 border border-stone-200 rounded-xl p-3 overflow-x-auto whitespace-pre-wrap">{chart}</pre>
  );
  if (!svg) return <div className="text-xs text-stone-400 py-2">Rendering diagram…</div>;
  return (
    <div className="w-full overflow-x-auto rounded-xl bg-white border border-stone-100 p-4 [&_svg]:max-w-full [&_svg]:h-auto"
      dangerouslySetInnerHTML={{ __html: svg }} />
  );
}

// ── helpers ───────────────────────────────────────────────────────────────────

async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  if (res.status === 204) return null;
  return res.json();
}

// Semantic color groups — all Tailwind classes present in source so JIT keeps them
const GROUP_COLORS = {
  models:    "bg-indigo-50 text-indigo-600 ring-indigo-200",
  training:  "bg-orange-50 text-orange-600 ring-orange-200",
  attention: "bg-rose-50 text-rose-600 ring-rose-200",
  inference: "bg-cyan-50 text-cyan-600 ring-cyan-200",
  memory:    "bg-amber-50 text-amber-600 ring-amber-200",
  data:      "bg-sky-50 text-sky-600 ring-sky-200",
  agents:    "bg-violet-50 text-violet-600 ring-violet-200",
  ops:       "bg-emerald-50 text-emerald-600 ring-emerald-200",
  meta:      "bg-stone-100 text-stone-500 ring-stone-200",
};
const DEFAULT_TAG_COLOR = "bg-stone-100 text-stone-500 ring-stone-200";

// Context — App owns tagGroups state; all children read from here
import { createContext, useContext } from "react";
const TagGroupsContext = createContext({});

async function fetchTagGroups() {
  try {
    return await fetch(`${API}/tag-colors`).then(r => r.json());
  } catch (_) {
    return {};
  }
}

function TagPill({ tag }) {
  const tagGroups = useContext(TagGroupsContext);
  const cls = GROUP_COLORS[tagGroups[tag]] || DEFAULT_TAG_COLOR;
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ring-1 ${cls}`}>
      {tag}
    </span>
  );
}

const VALID_TAGS = [
  "RAG", "Agents", "Serving", "MLOps", "LLM", "Inference",
  "VectorDB", "Attention", "KVCache", "Quantization", "FineTuning", "Embeddings", "Agentic",
];

// ── INGEST TAB ────────────────────────────────────────────────────────────────

function QueueSection({ onApproved, onExtractPreview }) {
  const [items, setItems] = useState([]);
  const [loaded, setLoaded] = useState(false);
  const [expandedId, setExpandedId] = useState(null);
  const [approvingId, setApprovingId] = useState(null);
  const [extractingId, setExtractingId] = useState(null);
  const [extractError, setExtractError] = useState(null); // { id, message }
  const [lastAction, setLastAction] = useState(null); // { type: 'saved'|'skipped', title, time }

  async function loadQueue() {
    try {
      const data = await api("/queue");
      setItems(data.items || []);
      setLoaded(true);
    } catch {}
  }

  useEffect(() => {
    loadQueue();
    const interval = setInterval(loadQueue, 10000);
    return () => clearInterval(interval);
  }, []);

  async function handleReject(id) {
    const title = items.find(i => i.id === id)?.title || "item";
    setApprovingId(id);
    try {
      await api(`/approve/${id}`, { method: "POST", body: JSON.stringify({ approved: false }) });
      await loadQueue();
      if (expandedId === id) setExpandedId(null);
      setLastAction({ type: "skipped", title, time: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) });
    } catch {}
    setApprovingId(null);
  }

  async function handleApprove(id) {
    const title = items.find(i => i.id === id)?.title || "item";
    setApprovingId(id);
    try {
      await api(`/approve/${id}`, { method: "POST", body: JSON.stringify({ approved: true }) });
      await loadQueue();
      onApproved?.();
      if (expandedId === id) setExpandedId(null);
      setLastAction({ type: "saved", title, time: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) });
    } catch {}
    setApprovingId(null);
  }

  async function handleQuickSave(item) {
    // Extract + save in one shot, no preview shown
    setExtractingId(item.id);
    setExtractError(null);
    try {
      await api(`/approve/${item.id}`, { method: "POST", body: JSON.stringify({ approved: false }) });
      const data = await api("/ingest-direct", { method: "POST", body: JSON.stringify({ url: item.url, force: true }) });
      await loadQueue();
      setLastAction({ type: "saved", title: data.title || item.url, time: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) });
    } catch (e) {
      try { await api("/queue-url", { method: "POST", body: JSON.stringify({ url: item.url, force: true }) }); } catch (_) {}
      await loadQueue();
      setExtractError({ id: item.id, message: e.message || "Quick save failed" });
    }
    setExtractingId(null);
  }

  async function handleExtract(item) {
    // For Share Sheet items: run extraction first, show preview in IngestTab
    setExtractingId(item.id);
    setExtractError(null);
    try {
      // First reject from queue so it doesn't show as duplicate
      await api(`/approve/${item.id}`, { method: "POST", body: JSON.stringify({ approved: false }) });
      // Now re-ingest with force to get preview
      const data = await api("/ingest", { method: "POST", body: JSON.stringify({ url: item.url, force: true }) });
      onExtractPreview?.(data);
      await loadQueue();
    } catch (e) {
      // Re-queue the item as pending_extraction so it's not lost
      try {
        await api("/queue-url", { method: "POST", body: JSON.stringify({ url: item.url, force: true }) });
      } catch (_) {}
      await loadQueue();
      setExtractError({ id: item.id, message: e.message || "Extraction failed" });
    }
    setExtractingId(null);
  }

  return (
    <div className="space-y-2">
      {/* Always-visible status bar */}
      <div className="flex items-center justify-between px-1">
        <div className="flex items-center gap-2">
          <div className={`flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 rounded-full ${
            items.length > 0 ? "bg-orange-50 text-orange-600 ring-1 ring-orange-200" : "bg-stone-100 text-stone-400"
          }`}>
            <span className={`w-1.5 h-1.5 rounded-full ${items.length > 0 ? "bg-orange-400 animate-pulse" : "bg-stone-300"}`} />
            Queue: {loaded ? items.length : "…"}
          </div>
          {lastAction && (
            <span className={`text-xs ${lastAction.type === "saved" ? "text-emerald-600" : "text-stone-400"}`}>
              {lastAction.type === "saved" ? "✓ Saved" : "Skipped"} · {lastAction.title.slice(0, 30)}{lastAction.title.length > 30 ? "…" : ""} · {lastAction.time}
            </span>
          )}
        </div>
        <button onClick={loadQueue} className="text-xs text-stone-400 hover:text-stone-600">↺</button>
      </div>
      {extractError && (
        <div className="flex items-start gap-2 bg-red-50 border border-red-100 text-red-600 rounded-xl px-3 py-2 text-xs">
          <span className="shrink-0 font-semibold">Extract failed:</span>
          <span className="flex-1">{extractError.message} — URL re-queued, try again.</span>
          <button onClick={() => setExtractError(null)} className="shrink-0 text-red-400 hover:text-red-600">✕</button>
        </div>
      )}
      {items.length === 0 && loaded && !lastAction && (
        <p className="text-xs text-stone-400 px-1">Nothing in queue — paste a URL above to ingest.</p>
      )}
      {items.map(item => {
        const title = item.title || item.url || "Untitled";
        const isExpanded = expandedId === item.id;
        const isPending = item.pending_extraction;
        const isBusy = approvingId === item.id || extractingId === item.id;
        const bullets = item.summary || [];
        const tags = item.tags || [];
        const page = item.suggested_page || "";

        return (
          <div key={item.id} className="bg-white rounded-2xl border border-stone-200 shadow-sm overflow-hidden">
            <div className="px-4 py-3 flex items-start gap-3 cursor-pointer" onClick={() => setExpandedId(isExpanded ? null : item.id)}>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  {isPending && (
                    <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-amber-100 text-amber-700">Share Sheet</span>
                  )}
                  <span className="text-sm font-medium text-stone-800 truncate">{title}</span>
                </div>
                {page && page !== "unprocessed" && <span className="text-xs font-mono text-stone-400">/{page}</span>}
              </div>
              <svg className={`w-4 h-4 text-stone-400 shrink-0 mt-0.5 transition-transform ${isExpanded ? "rotate-180" : ""}`} fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7"/></svg>
            </div>

            {isExpanded && (
              <div className="border-t border-stone-100">
                {isPending ? (
                  <div className="px-4 py-3 text-xs text-stone-500">
                    <span className="font-mono text-stone-400 break-all">{item.url}</span>
                    <p className="mt-1.5 text-amber-700">Tap "Extract" to fetch and preview the content before saving.</p>
                  </div>
                ) : (
                  <>
                    {bullets.length > 0 && (
                      <ul className="px-4 py-3 space-y-1.5">
                        {bullets.map((b, i) => (
                          <li key={i} className="flex items-start gap-2 text-xs text-stone-600">
                            <span className="shrink-0 w-4 h-4 rounded-full bg-orange-50 text-orange-500 text-[10px] font-semibold flex items-center justify-center mt-0.5">{i+1}</span>
                            {b}
                          </li>
                        ))}
                      </ul>
                    )}
                    {tags.length > 0 && (
                      <div className="px-4 pb-3 flex gap-1 flex-wrap">
                        {tags.map(t => <TagPill key={t} tag={t} />)}
                      </div>
                    )}
                  </>
                )}
                <div className="px-4 py-3 border-t border-stone-100 bg-stone-50/40 flex gap-2">
                  {isPending ? (
                    <>
                      <button onClick={() => handleQuickSave(item)} disabled={isBusy}
                        className="flex-1 py-2 bg-stone-900 text-white rounded-xl text-xs font-semibold hover:bg-stone-800 disabled:opacity-40 transition-colors">
                        {extractingId === item.id ? (
                          <span className="flex items-center justify-center gap-1.5">
                            <svg className="w-3 h-3 animate-spin" fill="none" viewBox="0 0 24 24"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"/><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg>
                            Saving…
                          </span>
                        ) : "Save now"}
                      </button>
                      <button onClick={() => handleExtract(item)} disabled={isBusy}
                        className="px-3 py-2 border border-stone-200 text-stone-600 rounded-xl text-xs font-medium hover:bg-stone-50 disabled:opacity-40 transition-colors">
                        Preview
                      </button>
                    </>
                  ) : (
                    <button onClick={() => handleApprove(item.id)} disabled={isBusy}
                      className="flex-1 py-2 bg-stone-900 text-white rounded-xl text-xs font-semibold hover:bg-stone-800 disabled:opacity-40 transition-colors">
                      {approvingId === item.id ? "Saving…" : "Save to wiki"}
                    </button>
                  )}
                  <button onClick={() => handleReject(item.id)} disabled={isBusy}
                    className="px-4 py-2 border border-stone-200 text-stone-500 rounded-xl text-xs font-medium hover:bg-stone-50 disabled:opacity-40 transition-colors">
                    Skip
                  </button>
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function HistorySection() {
  const [entries, setEntries] = useState([]);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    api("/history?limit=15").then(d => setEntries(d.entries || [])).catch(() => {});
  }, [open]);

  return (
    <div>
      <button onClick={() => setOpen(o => !o)}
        className="flex items-center gap-1.5 text-xs text-stone-400 hover:text-stone-600 px-1 transition-colors">
        <svg className={`w-3 h-3 transition-transform ${open ? "rotate-90" : ""}`} fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7"/></svg>
        Recent saves
      </button>
      {open && (
        <div className="mt-2 space-y-1">
          {entries.length === 0 && <p className="text-xs text-stone-400 px-1">No history yet.</p>}
          {entries.filter(e => e.type !== "delete").map((e, i) => {
            const page = e.written_to.replace(/^_wiki\/[^/]+\//, "").replace(/\.md$/, "");
            const tagsArr = e.tags ? e.tags.replace(/^\[|\]$/g, "").split(",").map(t => t.trim()).filter(Boolean) : [];
            const label = e.type === "ingest"
              ? page || e.source.split("/").pop() || "untitled"
              : e.type === "consolidate" ? `merge: ${e.merged}`
              : e.type === "insight" ? `Q: ${e.question.slice(0, 50)}`
              : e.type;
            return (
              <div key={i} className="flex items-start gap-2.5 px-3 py-2 bg-white border border-stone-100 rounded-xl text-xs">
                <span className="shrink-0 text-stone-300 font-mono">{e.ts.slice(5)}</span>
                <span className="flex-1 text-stone-600 truncate">{label}</span>
                {tagsArr.slice(0, 2).map(t => (
                  <span key={t} className="shrink-0 text-[10px] px-1.5 py-0.5 bg-stone-100 text-stone-500 rounded-full">{t}</span>
                ))}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function looksLikeQuestion(text) {
  if (!text) return false;
  const t = text.trim().toLowerCase();
  // Short text (no URL, no newlines) that reads like a question
  if (t.startsWith("http://") || t.startsWith("https://")) return false;
  if (t.includes("\n")) return false; // multi-line = likely notes/article
  if (t.length > 200) return false;   // long = likely content to capture
  if (t.endsWith("?")) return true;
  if (/^(what|how|why|when|who|where|explain|tell me|define|what's|whats|is there|can you|does|do |did |will |should )/.test(t)) return true;
  return false;
}

function IngestTab({ onApproved, onSwitchToChat }) {
  const [input, setInput] = useState("");
  const [images, setImages] = useState([]);
  const [loading, setLoading] = useState(false);
  const [preview, setPreview] = useState(null);
  const [edits, setEdits] = useState(null);
  const [error, setError] = useState("");
  const [approving, setApproving] = useState(false);
  const [done, setDone] = useState(null);
  const [openThread, setOpenThread] = useState(false);
  const [questionNudge, setQuestionNudge] = useState(false);
  const [deepResearch, setDeepResearch] = useState(false);
  const fileRef = useRef();
  const [queueKey, setQueueKey] = useState(0);

  function readFileAsImage(file) {
    return new Promise((resolve) => {
      const reader = new FileReader();
      reader.onload = (ev) => {
        const result = ev.target.result;
        const mediaType = result.split(";")[0].split(":")[1] || "image/png";
        const data = result.split(",")[1];
        resolve({ data, mediaType, name: file.name, previewUrl: result });
      };
      reader.readAsDataURL(file);
    });
  }

  async function handleImageUpload(e) {
    const files = Array.from(e.target.files);
    if (!files.length) return;
    const newImgs = await Promise.all(files.map(readFileAsImage));
    setImages(prev => [...prev, ...newImgs]);
    e.target.value = "";
  }

  async function handlePaste(e) {
    const items = Array.from(e.clipboardData?.items || []);
    const imgItems = items.filter(i => i.type.startsWith("image/"));
    if (!imgItems.length) return;
    e.preventDefault();
    const files = imgItems.map(i => i.getAsFile()).filter(Boolean);
    const newImgs = await Promise.all(files.map((f, i) =>
      readFileAsImage(Object.assign(f, { name: f.name || `screenshot-${i + 1}.png` }))
    ));
    setImages(prev => [...prev, ...newImgs]);
  }

  function removeImage(idx) { setImages(prev => prev.filter((_, i) => i !== idx)); }

  async function handleProcess() {
    if (!input.trim() && !images.length) return;
    setError(""); setPreview(null); setDone(null); setQuestionNudge(false);

    // Intercept question-like text — don't waste an ingest call on it
    if (!images.length && looksLikeQuestion(input)) {
      setQuestionNudge(true);
      return;
    }

    setLoading(true);
    try {
      const body = {};
      const trimmed = input.trim();
      const firstLine = trimmed.split("\n")[0].trim();
      if (firstLine.startsWith("http://") || firstLine.startsWith("https://")) body.url = firstLine;
      else if (trimmed) body.text = trimmed;
      if (images.length) body.images = images.map(({ data, mediaType }) => ({ data, mediaType }));
      if (deepResearch) body.deep_research = true;
      const data = await api("/ingest", { method: "POST", body: JSON.stringify(body) });
      setPreview(data); setEdits(null);
    } catch (e) { setError(e.message); }
    finally { setLoading(false); }
  }

  async function handleApprove(approved) {
    if (!preview) return;
    setApproving(true); setError("");
    try {
      const body = { approved, open_thread: approved && openThread };
      if (approved && edits) body.edits = edits;
      const data = await api(`/approve/${preview.id}`, { method: "POST", body: JSON.stringify(body) });
      const evoLabels = { extends: "🔵 extends understanding", refines: "🟡 refines understanding", supersedes: "🟠 supersedes old info", duplicates: "⚪ duplicate", contradicts: "🔴 contradiction flagged" };
      const evoMsg = data.evolution_type ? ` · ${evoLabels[data.evolution_type] || data.evolution_type}` : "";
      const doneMsg = approved
        ? `Saved to ${data.file_written}${evoMsg}${data.deep_dive_tagged ? " · 🔍 tagged for deeper research" : ""}`
        : "Skipped.";
      setDone(doneMsg);
      setPreview(null); setEdits(null); setInput(""); setImages([]); setOpenThread(false);
      if (approved) onApproved?.();
      setQueueKey(k => k + 1);
    } catch (e) { setError(e.message); }
    finally { setApproving(false); }
  }

  function startEditing() {
    const d = preview.diff_preview;
    setEdits({ title: d.title, summary: [...d.summary], suggested_page: d.suggested_page, suggested_wikilinks: [...d.suggested_wikilinks], tags: [...d.tags], diagram: d.diagram || "" });
  }
  function setEdit(field, value) { setEdits(e => ({ ...e, [field]: value })); }
  const display = edits || preview?.diff_preview;

  return (
    <div className="space-y-5">
      {done && (
        <div className="flex items-center gap-2.5 bg-emerald-50 border border-emerald-100 text-emerald-700 rounded-2xl px-4 py-3 text-sm">
          <svg className="w-4 h-4 shrink-0" fill="currentColor" viewBox="0 0 20 20"><path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clipRule="evenodd"/></svg>
          <span className="font-medium">{done}</span>
        </div>
      )}

      <div className="bg-white rounded-2xl border border-stone-200 shadow-sm overflow-hidden">
        <textarea
          className="w-full h-28 px-4 pt-4 text-sm text-stone-800 placeholder-stone-400 resize-none focus:outline-none"
          placeholder="Paste a URL, tweet, article, or notes… or paste a screenshot directly here"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onPaste={handlePaste}
        />

        {images.length > 0 && (
          <div className="flex flex-wrap gap-2 px-4 pb-3">
            {images.map((img, i) => (
              <div key={i} className="relative group w-14 h-14 rounded-xl overflow-hidden border border-stone-200">
                <img src={img.previewUrl} alt={img.name} className="w-full h-full object-cover" />
                <button onClick={() => removeImage(i)}
                  className="absolute top-0.5 right-0.5 bg-stone-900/70 text-white rounded-full w-4 h-4 text-[10px] hidden group-hover:flex items-center justify-center">
                  ×
                </button>
              </div>
            ))}
          </div>
        )}

        <div className="flex items-center justify-between gap-3 px-4 py-3 border-t border-stone-100 bg-stone-50/50">
          <div className="flex items-center gap-3">
            <button onClick={() => fileRef.current.click()}
              className="flex items-center gap-1.5 text-xs text-stone-500 hover:text-stone-700 transition-colors">
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13"/></svg>
              {images.length ? `${images.length} image${images.length > 1 ? "s" : ""} attached` : "Attach"}
            </button>
            <button onClick={() => setDeepResearch(v => !v)}
              title="Deep Research: analyzes content through 6 lenses"
              className={`flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-lg border transition-colors ${deepResearch ? "bg-violet-50 border-violet-200 text-violet-700 font-medium" : "border-stone-200 text-stone-400 hover:text-stone-600"}`}>
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z"/></svg>
              Deep
            </button>
          </div>
          <input ref={fileRef} type="file" accept="image/*" multiple className="hidden" onChange={handleImageUpload} />

          <button onClick={handleProcess} disabled={loading || (!input.trim() && !images.length)}
            className="px-5 py-2 bg-orange-500 text-white rounded-xl text-sm font-medium hover:bg-orange-600 disabled:opacity-40 disabled:cursor-not-allowed transition-colors shadow-sm">
            {loading ? (
              <span className="flex items-center gap-2">
                <svg className="w-3.5 h-3.5 animate-spin" fill="none" viewBox="0 0 24 24"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"/><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg>
                {deepResearch ? "Analyzing 6 lenses…" : "Processing…"}
              </span>
            ) : "Process"}
          </button>
        </div>
      </div>

      {questionNudge && (
        <div className="flex items-center justify-between gap-3 bg-stone-50 border border-stone-200 rounded-2xl px-4 py-3">
          <div className="flex items-center gap-2.5">
            <svg className="w-4 h-4 text-stone-400 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z"/></svg>
            <span className="text-sm text-stone-600">That looks like a question — ask your wiki in <span className="font-medium">Chat</span> instead.</span>
          </div>
          <button onClick={() => { onSwitchToChat?.(); setQuestionNudge(false); }}
            className="shrink-0 text-xs px-3 py-1.5 bg-stone-900 text-white rounded-xl font-medium hover:bg-stone-700 transition-colors">
            Go to Chat
          </button>
        </div>
      )}

      {error && (
        <div className="flex items-start gap-2.5 bg-red-50 border border-red-100 text-red-600 rounded-2xl px-4 py-3 text-sm">
          <svg className="w-4 h-4 mt-0.5 shrink-0" fill="currentColor" viewBox="0 0 20 20"><path fillRule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7 4a1 1 0 11-2 0 1 1 0 012 0zm-1-9a1 1 0 00-1 1v4a1 1 0 102 0V6a1 1 0 00-1-1z" clipRule="evenodd"/></svg>
          <span>{error}</span>
        </div>
      )}

      {!preview && <QueueSection key={queueKey} onApproved={() => { onApproved?.(); setQueueKey(k => k + 1); }} onExtractPreview={(data) => { setPreview(data); setEdits(null); setQueueKey(k => k + 1); }} />}
      {!preview && <HistorySection />}

      {preview && display && (
        <div className="bg-white rounded-2xl border border-stone-200 shadow-sm overflow-hidden">

          {/* Share Sheet badge */}
          {preview.pending_extraction && (
            <div className="flex items-center gap-2 px-5 py-2.5 bg-amber-50 border-b border-amber-100">
              <svg className="w-3.5 h-3.5 text-amber-500 shrink-0" fill="currentColor" viewBox="0 0 20 20"><path fillRule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clipRule="evenodd"/></svg>
              <span className="text-xs text-amber-700 font-medium">Queued from Share Sheet — extraction runs on approve</span>
            </div>
          )}

          {/* Title bar */}
          <div className="px-5 pt-5 pb-4 border-b border-stone-100">
            <div className="flex items-start justify-between gap-3">
              <div className="flex-1 min-w-0">
                {edits ? (
                  <input className="w-full text-base font-semibold text-stone-900 border border-orange-200 rounded-lg px-3 py-1.5 focus:outline-none focus:ring-2 focus:ring-orange-300"
                    value={edits.title} onChange={e => setEdit("title", e.target.value)} />
                ) : (
                  <h3 className="text-base font-semibold text-stone-900 leading-snug">{display.title}</h3>
                )}
                <div className="flex items-center gap-2 mt-2 flex-wrap">
                  {edits ? (
                    <input className="text-xs font-mono border border-orange-200 rounded-lg px-2 py-0.5 focus:outline-none focus:ring-1 focus:ring-orange-300 w-44"
                      value={edits.suggested_page} onChange={e => setEdit("suggested_page", e.target.value)} />
                  ) : (
                    <span className="text-xs font-mono text-stone-500 bg-stone-100 px-2 py-0.5 rounded-md">/{display.suggested_page}</span>
                  )}
                  {/* Depth badge */}
                  <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                    display.summary.length >= 7 ? "bg-violet-50 text-violet-600" :
                    display.summary.length >= 4 ? "bg-blue-50 text-blue-600" :
                    "bg-stone-100 text-stone-500"
                  }`}>
                    {display.summary.length >= 7 ? "Deep read" : display.summary.length >= 4 ? "Article" : "Quick note"} · {display.summary.length} insights
                  </span>
                </div>
              </div>
              <button onClick={() => edits ? setEdits(null) : startEditing()}
                className={`shrink-0 text-xs px-3 py-1.5 rounded-lg border font-medium transition-colors ${edits ? "border-stone-200 text-stone-500 hover:bg-stone-50" : "border-orange-200 text-orange-600 hover:bg-orange-50"}`}>
                {edits ? "Cancel" : "Edit"}
              </button>
            </div>
          </div>

          <div className="px-5 py-4 space-y-5">
            {/* Summary bullets */}
            <div>
              <p className="text-xs font-semibold text-stone-400 uppercase tracking-wider mb-2.5">Key insights</p>
              <ul className="space-y-2.5">
                {display.summary.map((b, i) => (
                  <li key={i} className="flex gap-3">
                    <span className="shrink-0 w-5 h-5 rounded-full bg-orange-50 text-orange-500 text-xs font-semibold flex items-center justify-center mt-0.5">{i + 1}</span>
                    {edits ? (
                      <textarea className="flex-1 text-sm border border-orange-200 rounded-lg px-2.5 py-1.5 resize-none focus:outline-none focus:ring-1 focus:ring-orange-300 leading-relaxed" rows={2}
                        value={edits.summary[i]}
                        onChange={e => { const s = [...edits.summary]; s[i] = e.target.value; setEdit("summary", s); }} />
                    ) : <span className="text-sm text-stone-700 leading-relaxed flex-1">{b}</span>}
                  </li>
                ))}
                {edits && (
                  <li>
                    <button onClick={() => setEdit("summary", [...edits.summary, ""])}
                      className="text-xs text-orange-500 hover:text-orange-600 flex items-center gap-1">
                      <span>+</span> Add insight
                    </button>
                  </li>
                )}
              </ul>
            </div>

            {/* Deep Research lenses */}
            {display.lenses && Object.keys(display.lenses).length > 0 && (
              <div>
                <p className="text-xs font-semibold text-stone-400 uppercase tracking-wider mb-2.5 flex items-center gap-1.5">
                  <svg className="w-3 h-3 text-violet-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z"/></svg>
                  6-Lens Analysis
                </p>
                <div className="grid grid-cols-1 gap-2">
                  {Object.entries(display.lenses).map(([key, lens]) => {
                    const confColor = lens.confidence === "high" ? "text-emerald-500" : lens.confidence === "medium" ? "text-amber-500" : "text-red-400";
                    const confDot = lens.confidence === "high" ? "🟢" : lens.confidence === "medium" ? "🟡" : "🔴";
                    return (
                      <div key={key} className="bg-stone-50 rounded-xl px-3.5 py-3 border border-stone-100">
                        <div className="flex items-center gap-1.5 mb-1">
                          <span className="text-xs font-semibold text-stone-700">{lens.label}</span>
                          <span className="text-[10px]">{confDot}</span>
                        </div>
                        <p className="text-xs text-stone-600 leading-relaxed">{lens.finding}</p>
                      </div>
                    );
                  })}
                </div>
                {display.synthesis && (
                  <div className="mt-2 bg-violet-50 rounded-xl px-3.5 py-3 border border-violet-100">
                    <p className="text-xs font-semibold text-violet-700 mb-1">Synthesis</p>
                    <p className="text-xs text-violet-800 leading-relaxed">{display.synthesis}</p>
                  </div>
                )}
                {display.open_questions?.length > 0 && (
                  <div className="mt-2">
                    <p className="text-xs font-semibold text-stone-400 mb-1">Open Questions</p>
                    <ul className="space-y-1">
                      {display.open_questions.map((q, i) => (
                        <li key={i} className="text-xs text-stone-500 flex gap-2">
                          <span className="text-stone-300 shrink-0">?</span>{q}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            )}

            {/* Tags + wikilinks row */}
            <div className="space-y-2.5">
              <div className="flex flex-wrap gap-1.5 items-center">
                {display.tags.map((t) => (
                  <span key={t} className="relative group">
                    <TagPill tag={t} />
                    {edits && (
                      <button onClick={() => setEdit("tags", edits.tags.filter(x => x !== t))}
                        className="absolute -top-1 -right-1 bg-stone-700 text-white rounded-full w-3.5 h-3.5 text-[9px] hidden group-hover:flex items-center justify-center">×</button>
                    )}
                  </span>
                ))}
                {edits && (
                  <select className="text-xs border border-stone-200 rounded-full px-2 py-0.5 text-stone-600 bg-white"
                    defaultValue=""
                    onChange={e => { const v = e.target.value; if (v && !edits.tags.includes(v)) setEdit("tags", [...edits.tags, v]); e.target.value = ""; }}>
                    <option value="">+ tag</option>
                    {VALID_TAGS.filter(t => !edits.tags.includes(t)).map(t => <option key={t}>{t}</option>)}
                  </select>
                )}
              </div>

              {(edits || display.suggested_wikilinks.length > 0) && (
                edits ? (
                  <input className="w-full text-xs border border-orange-200 rounded-lg px-3 py-1.5 focus:outline-none focus:ring-1 focus:ring-orange-300"
                    placeholder="related-concept, another-concept"
                    value={edits.suggested_wikilinks.join(", ")}
                    onChange={e => setEdit("suggested_wikilinks", e.target.value.split(",").map(s => s.trim()).filter(Boolean))} />
                ) : (
                  <div className="flex flex-wrap gap-1">
                    {display.suggested_wikilinks.map(w => (
                      <span key={w} className="text-xs font-mono text-stone-400 bg-stone-50 border border-stone-200 px-2 py-0.5 rounded-lg">[[{w}]]</span>
                    ))}
                  </div>
                )
              )}
            </div>

            {/* References */}
            {!edits && display.references && display.references.length > 0 && (
              <div className="space-y-1.5">
                <p className="text-xs font-semibold text-stone-400 uppercase tracking-wider">Links in this content</p>
                <div className="flex flex-col gap-1">
                  {display.references.map((ref, i) => {
                    let label = ref;
                    try { label = new URL(ref).hostname.replace(/^www\./, ""); } catch (_) {}
                    const isYT = ref.includes("youtube.com") || ref.includes("youtu.be");
                    const isGH = ref.includes("github.com");
                    const icon = isYT ? "▶" : isGH ? "⌥" : "↗";
                    return (
                      <a key={i} href={ref} target="_blank" rel="noopener noreferrer"
                        className="flex items-center gap-2 text-xs text-stone-600 hover:text-orange-600 transition-colors">
                        <span className="text-stone-400">{icon}</span>
                        <span className="truncate">{label}</span>
                      </a>
                    );
                  })}
                </div>
              </div>
            )}

            {/* Diagram */}
            {(display.diagram || edits) && (
              <div className="space-y-2">
                <p className="text-xs font-semibold text-stone-400 uppercase tracking-wider">Concept diagram</p>
                <div className="rounded-xl overflow-hidden border border-stone-100 bg-stone-50">
                  {edits ? (
                    <div className="space-y-2 p-3">
                      <textarea className="w-full text-xs font-mono border border-orange-200 rounded-lg px-3 py-2 resize-y focus:outline-none focus:ring-1 focus:ring-orange-300 bg-white"
                        rows={6} value={edits.diagram}
                        onChange={e => setEdit("diagram", e.target.value)}
                        placeholder="flowchart TD&#10;  A[Start] --> B[End]" />
                      {edits.diagram && <MermaidDiagram chart={edits.diagram} />}
                    </div>
                  ) : (
                    <div className="p-3">
                      <MermaidDiagram chart={display.diagram} />
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>

          {/* Actions */}
          <div className="px-5 py-4 border-t border-stone-100 bg-stone-50/40 space-y-3">
            <label className="flex items-center gap-2.5 cursor-pointer select-none group">
              <div className={`w-8 h-4 rounded-full transition-colors ${openThread ? "bg-orange-500" : "bg-stone-200"}`}
                onClick={() => setOpenThread(v => !v)}>
                <div className={`w-3.5 h-3.5 bg-white rounded-full shadow transition-transform mt-0.5 ml-0.5 ${openThread ? "translate-x-3.5" : ""}`} />
              </div>
              <span className="text-xs text-stone-500 group-hover:text-stone-700 transition-colors">
                Flag for deeper research <span className="text-stone-400">(adds 🔍 tag)</span>
              </span>
            </label>
            <div className="flex gap-2">
              <button onClick={() => handleApprove(true)} disabled={approving}
                className="flex-1 py-2.5 bg-stone-900 text-white rounded-xl text-sm font-semibold hover:bg-stone-800 disabled:opacity-40 transition-colors">
                {approving ? "Saving…" : "Save to wiki"}
              </button>
              <button onClick={() => handleApprove(false)} disabled={approving}
                className="px-5 py-2.5 border border-stone-200 text-stone-500 rounded-xl text-sm font-medium hover:bg-stone-50 disabled:opacity-40 transition-colors">
                Skip
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ── CHAT TAB ──────────────────────────────────────────────────────────────────

function ChatTab() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [thinking, setThinking] = useState("");
  const [savedIdx, setSavedIdx] = useState(new Set());
  const bottomRef = useRef();

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, thinking]);

  async function handleSend() {
    if (!input.trim() || loading) return;
    const userMsg = input.trim();
    setInput("");
    setMessages(prev => [...prev, { role: "user", content: userMsg }]);
    setLoading(true); setThinking("Searching wiki…");
    try {
      const history = messages.map(m => ({ role: m.role, content: m.content }));
      const data = await api("/chat", { method: "POST", body: JSON.stringify({ message: userMsg, history }) });
      setThinking("");
      setMessages(prev => [...prev, { role: "assistant", content: data.answer, sources: data.sources, pages_read: data.pages_read, knowledge_card: data.knowledge_card || null, question: userMsg }]);
    } catch (e) {
      setThinking("");
      setMessages(prev => [...prev, { role: "assistant", content: `Error: ${e.message}`, sources: [], isError: true }]);
    } finally { setLoading(false); }
  }

  async function handleSaveAnswer(msg, idx) {
    try {
      await api("/save-answer", { method: "POST", body: JSON.stringify({ question: msg.question || "Chat insight", answer: msg.content, sources: msg.sources || [], pages_read: msg.pages_read || [] }) });
      setSavedIdx(prev => new Set([...prev, idx]));
    } catch (e) { alert(`Save failed: ${e.message}`); }
  }

  function handleKey(e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend(); }
  }

  return (
    <div className="flex flex-col h-full">
      <div className="flex-1 overflow-y-auto space-y-4 min-h-0 pb-2">
        {messages.length === 0 && (
          <div className="flex flex-col items-center justify-center h-full pt-16 space-y-3">
            <div className="w-10 h-10 bg-stone-100 rounded-2xl flex items-center justify-center">
              <svg className="w-5 h-5 text-stone-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z"/></svg>
            </div>
            <p className="text-sm text-stone-400">Ask anything about your knowledge base</p>
          </div>
        )}

        {messages.map((msg, i) => (
          <div key={i} className={`flex flex-col ${msg.role === "user" ? "items-end" : "items-start"} gap-1`}>
            {msg.role === "user" ? (
              <div className="max-w-[78%] bg-orange-500 text-white text-sm px-4 py-2.5 rounded-2xl rounded-tr-sm leading-relaxed">
                {msg.content}
              </div>
            ) : (
              <div className="w-full space-y-2">
                {msg.isError ? (
                  <div className="text-sm text-red-500 bg-red-50 border border-red-100 rounded-2xl px-4 py-3">
                    {msg.content}
                  </div>
                ) : (
                  <div
                    className="text-sm text-stone-800 leading-relaxed prose prose-sm max-w-none
                      prose-headings:text-stone-900 prose-headings:font-semibold prose-headings:mt-3 prose-headings:mb-1
                      prose-h1:text-base prose-h2:text-sm prose-h3:text-xs prose-h3:uppercase prose-h3:tracking-wide prose-h3:text-stone-500
                      prose-p:my-1.5 prose-p:text-stone-700
                      prose-strong:text-stone-900 prose-strong:font-semibold
                      prose-ul:my-1.5 prose-ul:pl-4 prose-ol:my-1.5 prose-ol:pl-4
                      prose-li:my-0.5 prose-li:text-stone-700
                      prose-code:text-orange-600 prose-code:bg-orange-50 prose-code:rounded prose-code:px-1 prose-code:text-xs
                      prose-a:text-orange-500 prose-a:no-underline hover:prose-a:underline"
                    dangerouslySetInnerHTML={{ __html: marked.parse(msg.content) }}
                  />
                )}
                {msg.knowledge_card && <KnowledgeCard card={msg.knowledge_card} />}
                <div className="flex items-center gap-2 flex-wrap">
                  {msg.pages_read?.length > 0 && (
                    <div className="flex items-center gap-1 flex-wrap flex-1">
                      {msg.pages_read.map(p => (
                        <span key={p} className="text-[11px] text-stone-400 font-mono bg-stone-100 px-1.5 py-0.5 rounded-md">[[{p}]]</span>
                      ))}
                    </div>
                  )}
                  {!msg.isError && (
                    <button onClick={() => handleSaveAnswer(msg, i)} disabled={savedIdx.has(i)}
                      className={`ml-auto shrink-0 text-xs px-2.5 py-1 rounded-full border transition-colors whitespace-nowrap ${
                        savedIdx.has(i) ? "border-emerald-200 text-emerald-600 bg-emerald-50" : "border-stone-200 text-stone-400 hover:border-stone-300 hover:text-stone-600"
                      }`}>
                      {savedIdx.has(i) ? "✓ Saved" : "Save to wiki"}
                    </button>
                  )}
                </div>
              </div>
            )}
          </div>
        ))}

        {thinking && (
          <div className="flex justify-start items-center gap-2">
            <div className="w-7 h-7 rounded-full bg-stone-100 flex items-center justify-center shrink-0">
              <svg className="w-3.5 h-3.5 text-stone-500 animate-spin" fill="none" viewBox="0 0 24 24"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"/><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg>
            </div>
            <span className="text-sm text-stone-400">{thinking}</span>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      <div className="mt-3 bg-white border border-stone-200 rounded-2xl shadow-sm overflow-hidden">
        <textarea
          className="w-full px-4 pt-3 pb-2 text-sm text-stone-800 placeholder-stone-400 resize-none focus:outline-none"
          rows={2}
          placeholder="Ask your wiki…"
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={handleKey}
        />
        <div className="flex justify-end px-3 py-2 border-t border-stone-100 bg-stone-50/50">
          <button onClick={handleSend} disabled={!input.trim() || loading}
            className="px-4 py-1.5 bg-orange-500 text-white rounded-xl text-sm font-medium hover:bg-orange-600 disabled:opacity-40 transition-colors">
            Send
          </button>
        </div>
      </div>
    </div>
  );
}

// ── CONCEPT PAGE VIEW ─────────────────────────────────────────────────────────

const EVO_COLORS = {
  "🔵": "bg-blue-50 border-blue-200 text-blue-800",
  "🟡": "bg-amber-50 border-amber-200 text-amber-800",
  "🟠": "bg-orange-50 border-orange-200 text-orange-800",
  "🔴": "bg-red-50 border-red-200 text-red-800",
  "⚪": "bg-stone-50 border-stone-200 text-stone-600",
};
const EVO_LABELS = { "🔵": "extends", "🟡": "refines", "🟠": "supersedes", "🔴": "contradicts", "⚪": "duplicate" };

function ConceptPageView({ page }) {
  const [expandedSections, setExpandedSections] = useState(new Set([0]));
  const toggleSection = i => setExpandedSections(prev => {
    const n = new Set(prev); n.has(i) ? n.delete(i) : n.add(i); return n;
  });

  const evoColor = EVO_COLORS[page.evolution_badge] || EVO_COLORS["🔵"];
  const tags = Array.isArray(page.tags) ? page.tags : [];
  const sections = page.sections || [];
  const activeSections = sections.filter(s => !s.superseded);
  const supersededSections = sections.filter(s => s.superseded);

  return (
    <div className="space-y-4 pb-6">
      {/* Understanding block */}
      <div className={`rounded-2xl border p-4 ${evoColor}`}>
        <div className="flex items-start justify-between gap-3 mb-2">
          <div className="flex items-center gap-2">
            <span className="text-lg">{page.evolution_badge}</span>
            <span className="text-xs font-semibold uppercase tracking-wide opacity-70">Current understanding</span>
          </div>
          <div className="flex items-center gap-1.5 shrink-0">
            <span className="text-xs opacity-60">v{page.understanding_version}</span>
            <span className="text-xs opacity-50">·</span>
            <span className="text-xs opacity-60">{page.entry_count} {page.entry_count === 1 ? "source" : "sources"}</span>
          </div>
        </div>
        <p className="text-sm leading-relaxed font-medium">
          {page.current_understanding || "No understanding captured yet."}
        </p>
        {page.evolution_note && (
          <p className="text-xs mt-2 opacity-60 italic">{page.evolution_note}</p>
        )}
      </div>

      {/* Tags */}
      {tags.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {tags.map(t => <TagPill key={t} tag={t} />)}
        </div>
      )}

      {/* Any standalone diagrams (not in a section) */}
      {page.diagrams?.length > 0 && activeSections.every(s => !s.diagram) && (
        <div className="rounded-xl border border-stone-100 bg-stone-50 p-3">
          <p className="text-xs font-medium text-stone-500 mb-2">Diagram</p>
          <MermaidDiagram chart={page.diagrams[0]} />
        </div>
      )}

      {/* Source sections */}
      {activeSections.length > 0 && (
        <div>
          <p className="text-xs font-semibold text-stone-400 uppercase tracking-wide mb-2">Evidence ({activeSections.length})</p>
          <div className="space-y-2">
            {activeSections.map((sec, i) => (
              <div key={i} className="rounded-xl border border-stone-100 bg-white overflow-hidden">
                <button onClick={() => toggleSection(i)}
                  className="w-full flex items-center justify-between px-4 py-2.5 hover:bg-stone-50 transition-colors text-left">
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="text-xs font-medium text-stone-700 truncate">
                      {sec.url ? <a href={sec.url} target="_blank" rel="noreferrer" onClick={e => e.stopPropagation()} className="hover:text-orange-600 transition-colors">{sec.title || sec.url}</a> : (sec.title || "Source")}
                    </span>
                  </div>
                  <div className="flex items-center gap-2 shrink-0 ml-2">
                    {sec.date && <span className="text-[10px] text-stone-400">{sec.date}</span>}
                    <svg className={`w-3.5 h-3.5 text-stone-400 transition-transform ${expandedSections.has(i) ? "rotate-180" : ""}`} fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7"/></svg>
                  </div>
                </button>
                {expandedSections.has(i) && (
                  <div className="px-4 pb-3 border-t border-stone-50 space-y-2">
                    {sec.bullets?.length > 0 && (
                      <ul className="space-y-1 mt-2">
                        {sec.bullets.map((b, j) => (
                          <li key={j} className="flex gap-2 text-xs text-stone-600">
                            <span className="text-orange-400 shrink-0 mt-0.5">•</span>
                            <span>{b}</span>
                          </li>
                        ))}
                      </ul>
                    )}
                    {sec.key_insight && (
                      <p className="text-xs text-stone-500 border-l-2 border-orange-200 pl-2 italic mt-1">{sec.key_insight}</p>
                    )}
                    {sec.diagram && <MermaidDiagram chart={sec.diagram} />}
                    {sec.related?.length > 0 && (
                      <div className="flex flex-wrap gap-1 mt-1">
                        {sec.related.map(r => (
                          <span key={r} className="text-[10px] px-1.5 py-0.5 rounded bg-stone-100 text-stone-500">[[{r}]]</span>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Superseded sections — collapsed by default */}
      {supersededSections.length > 0 && (
        <details className="group">
          <summary className="text-xs text-stone-400 cursor-pointer hover:text-stone-600 transition-colors list-none flex items-center gap-1">
            <svg className="w-3 h-3 group-open:rotate-90 transition-transform" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7"/></svg>
            {supersededSections.length} superseded {supersededSections.length === 1 ? "entry" : "entries"}
          </summary>
          <div className="mt-2 space-y-2 border-l-2 border-amber-200 pl-3">
            {supersededSections.map((sec, i) => (
              <div key={i} className="rounded-xl border border-amber-100 bg-amber-50/50 px-4 py-2.5">
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-xs font-medium text-amber-700 opacity-70">{sec.title || "Source"}</span>
                  {sec.date && <span className="text-[10px] text-amber-400">{sec.date}</span>}
                </div>
                {sec.superseded_reason && <p className="text-xs text-amber-600 italic">Superseded: {sec.superseded_reason}</p>}
                {sec.key_insight && <p className="text-xs text-stone-400 mt-1 line-through">{sec.key_insight}</p>}
              </div>
            ))}
          </div>
        </details>
      )}

      {/* Backlinks */}
      {page.backlinks?.length > 0 && (
        <div className="pt-3 mt-3 border-t border-stone-100">
          <p className="text-[10px] font-semibold text-stone-400 uppercase tracking-wider mb-1.5">Referenced by</p>
          <div className="flex flex-wrap gap-1">
            {page.backlinks.map(bl => (
              <span key={bl} className="text-[11px] px-2 py-0.5 rounded-lg bg-indigo-50 text-indigo-600 border border-indigo-100">[[{bl}]]</span>
            ))}
          </div>
        </div>
      )}

      {/* Quick note */}
      <QuickNoteInline pageName={page.name} />

      {/* Follow-up reads */}
      <FollowUpReads pageName={page.name || page.pageName} onOpenPage={page._onOpenPage} />
    </div>
  );
}

function QuickNoteInline({ pageName }) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);

  async function save() {
    if (!note.trim()) return;
    setSaving(true);
    try {
      await api("/quick-note", { method: "POST", body: JSON.stringify({ page: pageName, note }) });
      setNote(""); setOpen(false);
    } catch (e) { alert(e.message); }
    finally { setSaving(false); }
  }

  return (
    <div className="pt-3 mt-3 border-t border-stone-100">
      {!open ? (
        <button onClick={() => setOpen(true)}
          className="text-xs text-stone-400 hover:text-stone-600 flex items-center gap-1.5 transition-colors">
          <span className="text-base leading-none">💭</span> Add a quick thought
        </button>
      ) : (
        <div className="space-y-2">
          <textarea
            autoFocus
            className="w-full text-sm border border-stone-200 rounded-xl px-3 py-2 resize-none focus:outline-none focus:ring-2 focus:ring-orange-200 focus:border-orange-300 bg-white"
            rows={3}
            placeholder="Something you realised, a connection, a question…"
            value={note}
            onChange={e => setNote(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) save(); }}
          />
          <div className="flex gap-2">
            <button onClick={save} disabled={saving || !note.trim()}
              className="text-xs px-3 py-1.5 bg-stone-900 text-white rounded-lg hover:bg-stone-800 disabled:opacity-40 transition-colors">
              {saving ? "Saving…" : "Save ⌘↵"}
            </button>
            <button onClick={() => { setOpen(false); setNote(""); }}
              className="text-xs px-3 py-1.5 border border-stone-200 text-stone-500 rounded-lg hover:bg-stone-50 transition-colors">
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function FollowUpReads({ pageName, onOpenPage }) {
  const [suggestions, setSuggestions] = useState(null);

  useEffect(() => {
    if (!pageName) return;
    const history = JSON.parse(localStorage.getItem("sw_read_history") || "[]")
      .filter(n => n !== pageName).slice(0, 5);
    const qs = history.length ? `?recently_read=${history.join(",")}` : "";
    api(`/follow-up/${pageName}${qs}`)
      .then(d => setSuggestions(d.suggestions || []))
      .catch(() => setSuggestions([]));
  }, [pageName]);

  if (!suggestions || suggestions.length === 0) return null;

  return (
    <div className="pt-4 mt-4 border-t border-stone-100">
      <p className="text-[10px] font-semibold text-stone-400 uppercase tracking-wider mb-2.5">Continue reading</p>
      <div className="space-y-1.5">
        {suggestions.map(s => (
          <button key={s.name} onClick={() => onOpenPage?.(s.name)}
            className="w-full text-left flex items-center justify-between gap-3 px-3 py-2.5 rounded-xl border border-stone-100 hover:border-orange-200 hover:bg-orange-50/40 transition-all group">
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium text-stone-800 truncate group-hover:text-orange-700">{s.title}</span>
                <span className="shrink-0 text-[10px] text-stone-400 bg-stone-100 px-1.5 py-0.5 rounded-full">{s.entry_count}</span>
              </div>
              <p className="text-[10px] text-stone-400 mt-0.5">{s.reason}</p>
            </div>
            <svg className="w-3.5 h-3.5 text-stone-300 group-hover:text-orange-400 shrink-0 transition-colors" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7"/>
            </svg>
          </button>
        ))}
      </div>
    </div>
  );
}

// ── KNOWLEDGE CARD (Chat) ──────────────────────────────────────────────────────

function KnowledgeCard({ card }) {
  const [expanded, setExpanded] = useState(false);
  const evoColor = EVO_COLORS[card.evolution_badge] || EVO_COLORS["🔵"];
  const tags = Array.isArray(card.tags) ? card.tags : [];
  const sections = card.sections || [];

  return (
    <div className="mt-3 rounded-2xl border border-stone-200 overflow-hidden bg-white">
      {/* Understanding header */}
      <div className={`px-4 py-3 border-b ${evoColor}`}>
        <div className="flex items-center justify-between mb-1">
          <div className="flex items-center gap-1.5">
            <span>{card.evolution_badge || "🔵"}</span>
            <span className="text-xs font-semibold">{card.title}</span>
          </div>
          <span className="text-[10px] opacity-60">v{card.understanding_version} · {card.entry_count} sources</span>
        </div>
        <p className="text-xs leading-relaxed">{card.current_understanding || "No current understanding yet."}</p>
        {card.evolution_note && <p className="text-[10px] mt-1 opacity-60 italic">{card.evolution_note}</p>}
      </div>

      {/* Tags */}
      {tags.length > 0 && (
        <div className="flex flex-wrap gap-1 px-4 py-2 border-b border-stone-100">
          {tags.map(t => <TagPill key={t} tag={t} />)}
        </div>
      )}

      {/* Expand toggle for sources + diagrams */}
      <button onClick={() => setExpanded(v => !v)}
        className="w-full flex items-center justify-between px-4 py-2 text-xs text-stone-400 hover:text-stone-600 hover:bg-stone-50 transition-colors">
        <span>{sections.length} source{sections.length !== 1 ? "s" : ""}{card.diagrams?.length ? ` · ${card.diagrams.length} diagram${card.diagrams.length > 1 ? "s" : ""}` : ""}</span>
        <svg className={`w-3.5 h-3.5 transition-transform ${expanded ? "rotate-180" : ""}`} fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7"/></svg>
      </button>

      {expanded && (
        <div className="px-4 pb-3 space-y-3 border-t border-stone-50">
          {/* Diagrams */}
          {card.diagrams?.map((d, i) => (
            <MermaidDiagram key={i} chart={d} />
          ))}
          {/* Source timeline */}
          {sections.filter(s => !s.superseded).map((sec, i) => (
            <div key={i} className="border-l-2 border-stone-100 pl-3 py-1">
              <div className="flex items-center gap-2 mb-1">
                {sec.url
                  ? <a href={sec.url} target="_blank" rel="noreferrer" className="text-xs font-medium text-orange-600 hover:underline truncate">{sec.title || sec.url}</a>
                  : <span className="text-xs font-medium text-stone-600">{sec.title || "Source"}</span>}
                {sec.date && <span className="text-[10px] text-stone-400 shrink-0">{sec.date}</span>}
              </div>
              {sec.key_insight && <p className="text-xs text-stone-500 italic">{sec.key_insight}</p>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── BROWSE TAB ────────────────────────────────────────────────────────────────

// Stable key from item text — survives re-runs (same suggestion = same key)
function _healthKey(text) {
  let h = 0;
  for (let i = 0; i < Math.min(text.length, 120); i++) {
    h = (Math.imul(31, h) + text.charCodeAt(i)) | 0;
  }
  return "hk_" + Math.abs(h).toString(36);
}

// localStorage helpers — persist acked/applied items across sessions
const LS_HEALTH = "sw_health_acked";
function loadHealthAcked() {
  try { return JSON.parse(localStorage.getItem(LS_HEALTH) || "{}"); } catch { return {}; }
}
function saveHealthAcked(map) {
  try { localStorage.setItem(LS_HEALTH, JSON.stringify(map)); } catch {}
}

// Build a flat list of all AUTO-APPLICABLE actions from a lint report.
// missing_connections are intentionally excluded — /fix-page can't insert wikilinks,
// so showing Apply for them was misleading. They display as read-only hints.
function buildActions(report, onFix, existingPages = new Set()) {
  const pageExists = slug => existingPages.size === 0 || existingPages.has(slug);
  const actions = [];
  (report.quick_wins || []).forEach((w, i) => {
    const pageMatches = [...w.matchAll(/\[\[([^\]]+)\]\]/g)].map(m => m[1]);
    const wl = w.toLowerCase();
    const isMerge = wl.includes("merge");
    const isCreate = wl.includes("create") || wl.includes("stub") || wl.includes("new page");
    // Only treat as fix if it's about normalizing existing links/structure (not adding new content)
    const isFix = !isCreate && (wl.includes("standardis") || wl.includes("kebab") || wl.includes("sort") || wl.includes("recount"));
    if (isMerge && pageMatches.length >= 2 && pageExists(pageMatches[0]) && pageExists(pageMatches[1])) {
      actions.push({ key: `win-${i}`, type: "merge", label: w, source: pageMatches[0], target: pageMatches[1] });
    } else if (isFix && pageMatches[0] && onFix && pageExists(pageMatches[0])) {
      actions.push({ key: `win-${i}`, type: "fix", label: w, page: pageMatches[0] });
    }
  });
  (report.inconsistencies || []).forEach((inc, i) => {
    const pages = Array.isArray(inc.pages) ? inc.pages : [inc.pages];
    if (pages.length === 2 && pageExists(pages[0]) && pageExists(pages[1])) {
      actions.push({ key: `inc-${i}`, type: "merge", label: `Merge ${pages[0]} + ${pages[1]}: ${inc.issue}`, source: pages[0], target: pages[1] });
    }
  });
  // missing_connections: read-only — not added here, rendered separately without Apply button
  return actions;
}

function LintPanel({ onClose, onConsolidate, onFix }) {
  const [running, setRunning] = useState(false);
  const [report, setReport] = useState(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState(new Set());
  // ackedMap: { stableKey: { status: "applied"|"noted", date: "YYYY-MM-DD" } }
  // persisted to localStorage so it survives refreshes and re-runs
  const [ackedMap, setAckedMap] = useState(() => loadHealthAcked());
  const [applyingAll, setApplyingAll] = useState(false);
  const [applyProgress, setApplyProgress] = useState({ done: 0, total: 0 });
  const [currentKey, setCurrentKey] = useState(null);
  const [existingPages, setExistingPages] = useState(new Set());

  function markAcked(text, status) {
    const key = _healthKey(text);
    const date = new Date().toISOString().slice(0, 10);
    setAckedMap(prev => {
      const next = { ...prev, [key]: { status, date } };
      saveHealthAcked(next);
      return next;
    });
  }
  function isAcked(text) { return !!ackedMap[_healthKey(text)]; }
  function ackedInfo(text) { return ackedMap[_healthKey(text)] || null; }

  // Load cached result immediately on open — no Sonnet call
  useEffect(() => {
    fetch(`${API}/lint`)
      .then(r => r.status === 204 ? null : r.json())
      .then(data => { if (data) setReport(data); })
      .catch(() => {});
    // Fetch known page slugs so we can filter out actions on non-existent pages
    fetch(`${API}/pages?folder=concepts`)
      .then(r => r.json())
      .then(d => setExistingPages(new Set((d.pages || []).map(p => p.name))))
      .catch(() => {});
  }, []);

  // Reset selections when report changes (but keep ackedMap — it's content-keyed)
  useEffect(() => {
    setSelected(new Set());
  }, [report]);

  async function runLint(save = false) {
    setRunning(true); setError("");
    try {
      const data = await api("/lint", { method: "POST", body: JSON.stringify({ save }) });
      setReport(data);
      if (save) setSaved(true);
    } catch (e) { setError(e.message); }
    finally { setRunning(false); setSaving(false); }
  }

  const scoreColor = s => s >= 80 ? "text-emerald-600" : s >= 60 ? "text-amber-600" : "text-red-500";
  const scoreBg = s => s >= 80 ? "bg-emerald-50 border-emerald-100" : s >= 60 ? "bg-amber-50 border-amber-100" : "bg-red-50 border-red-100";

  const actions = report ? buildActions(report, onFix, existingPages) : [];
  const pendingActions = actions.filter(a => !isAcked(a.label));
  const allSelected = pendingActions.length > 0 && pendingActions.every(a => selected.has(a.key));

  function toggleAction(key) {
    setSelected(prev => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });
  }

  function toggleSelectAll() {
    if (allSelected) {
      setSelected(new Set());
    } else {
      setSelected(new Set(pendingActions.map(a => a.key)));
    }
  }

  async function applySelected() {
    const toApply = actions.filter(a => selected.has(a.key) && !isAcked(a.label));
    const total = toApply.length;
    setApplyingAll(true);
    setApplyProgress({ done: 0, total });
    const failures = [];
    for (const action of toApply) {
      setCurrentKey(action.key);
      try {
        if (action.type === "merge") {
          await api("/consolidate", { method: "POST", body: JSON.stringify({ source: action.source, target: action.target }) });
        } else if (action.type === "fix") {
          await api(`/fix-page/${encodeURIComponent(action.page)}`, { method: "POST" });
        }
        markAcked(action.label, "applied");
        setSelected(prev => { const n = new Set(prev); n.delete(action.key); return n; });
        setApplyProgress(prev => ({ ...prev, done: prev.done + 1 }));
      } catch (e) {
        failures.push(`• ${action.label}: ${e.message}`);
        setApplyProgress(prev => ({ ...prev, done: prev.done + 1 }));
        // Continue applying remaining actions
      }
    }
    setCurrentKey(null);
    setApplyingAll(false);
    if (failures.length > 0) {
      alert(`${total - failures.length}/${total} applied.\n\nFailed:\n${failures.join("\n")}`);
    }
  }

  return (
    <div className="bg-white border border-stone-200 rounded-2xl shadow-sm overflow-hidden mb-4 flex flex-col max-h-[60vh]">
      <div className="flex items-center justify-between px-4 py-3 border-b border-stone-100 bg-stone-50/50 shrink-0">
        <div>
          <h3 className="text-sm font-semibold text-stone-800">Wiki Health Check</h3>
          {report?.ran_at && !running && (
            <p className="text-[10px] text-stone-400 mt-0.5">Last run {report.ran_at}</p>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button onClick={() => runLint(false)} disabled={running}
            className="text-xs px-2.5 py-1 rounded-lg border border-stone-200 text-stone-500 hover:bg-stone-50 disabled:opacity-40 transition-colors">
            {running ? (
              <span className="flex items-center gap-1">
                <svg className="w-3 h-3 animate-spin" fill="none" viewBox="0 0 24 24"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"/><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg>
                Scanning…
              </span>
            ) : report ? "Re-run" : "Run"}
          </button>
          <button onClick={onClose} className="w-6 h-6 flex items-center justify-center text-stone-400 hover:text-stone-600 rounded-lg hover:bg-stone-100 text-xs">✕</button>
        </div>
      </div>

      <div className="p-4 space-y-4 overflow-y-auto flex-1">
        {!report && !running && (
          <p className="text-xs text-stone-500">No report yet — hit Run to scan all concept pages.</p>
        )}
        {error && <p className="text-xs text-red-500">{error}</p>}

        {report && (
          <div className="space-y-3">
            <div className="flex items-center gap-3 flex-wrap">
              <div className={`inline-flex items-center gap-2 px-3 py-1.5 rounded-xl border ${scoreBg(report.health_score)}`}>
                <span className={`text-xl font-bold ${scoreColor(report.health_score)}`}>{report.health_score}</span>
                <span className="text-xs text-stone-500">/ 100 · {report.pages_scanned} pages</span>
              </div>
            </div>

            {report.category_scores && Object.keys(report.category_scores).length > 0 && (
              <div>
                <p className="text-xs font-semibold text-stone-500 uppercase tracking-wider mb-2">By category</p>
                <div className="flex flex-wrap gap-2">
                  {Object.entries(report.category_scores).sort((a, b) => b[1] - a[1]).map(([group, score]) => (
                    <div key={group} className={`flex items-center gap-1.5 px-2.5 py-1 rounded-lg border text-xs ${scoreBg(score)}`}>
                      <span className="text-stone-500 capitalize">{group}</span>
                      <span className={`font-bold ${scoreColor(score)}`}>{score}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Legend */}
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-stone-400 border-t border-stone-100 pt-2">
              <span>⚡ <span className="text-stone-500">Quick wins</span> — selectable, auto-applied via Apply button</span>
              <span>⚠ 🔗 📝 🏝 — read-only insights, mark done manually after you fix them</span>
            </div>

            {report.quick_wins?.length > 0 && (
              <div>
                <p className="text-xs font-semibold text-stone-600 mb-1.5">⚡ Quick wins <span className="font-normal text-stone-400 ml-1">— auto-applicable</span></p>
                <ul className="space-y-1.5">
                  {report.quick_wins.map((w, i) => {
                    const key = `win-${i}`;
                    const action = actions.find(a => a.key === key);
                    const acked = ackedInfo(action ? action.label : w);
                    const isDone = !!acked;
                    return (
                      <li key={i} className={`text-xs flex items-start gap-2 ${isDone ? "opacity-40" : "text-stone-600"}`}>
                        {action && !isDone ? (
                          <input type="checkbox" checked={selected.has(key)} onChange={() => toggleAction(key)}
                            className="mt-0.5 shrink-0 accent-orange-500 cursor-pointer" />
                        ) : <span className="w-3.5 shrink-0" />}
                        <span className={`flex-1 ${isDone ? "line-through" : ""}`}>• {w}</span>
                        {isDone
                          ? <span className="shrink-0 text-emerald-600 text-[10px] whitespace-nowrap">✓ {acked.status} {acked.date}</span>
                          : !action && <button onClick={() => markAcked(w, "noted")} className="shrink-0 text-[10px] text-stone-400 hover:text-stone-600 border border-stone-200 rounded px-1">Mark done</button>
                        }
                        {currentKey === key && <span className="shrink-0 text-orange-500 text-[10px]">running…</span>}
                      </li>
                    );
                  })}
                </ul>
              </div>
            )}

            {report.inconsistencies?.length > 0 && (
              <div>
                <p className="text-xs font-semibold text-red-600 mb-1.5">⚠ Inconsistencies <span className="font-normal text-stone-400 ml-1">— edit pages manually, then mark done</span></p>
                <ul className="space-y-1.5">
                  {report.inconsistencies.map((inc, i) => {
                    const pages = Array.isArray(inc.pages) ? inc.pages : [inc.pages];
                    const key = `inc-${i}`;
                    const action = actions.find(a => a.key === key);
                    const itemText = `${pages.join("+")}:${inc.issue}`;
                    const acked = ackedInfo(action ? action.label : itemText);
                    const isDone = !!acked;
                    return (
                      <li key={i} className={`text-xs flex items-start gap-2 ${isDone ? "opacity-40" : "text-stone-600"}`}>
                        {action && !isDone ? (
                          <input type="checkbox" checked={selected.has(key)} onChange={() => toggleAction(key)}
                            className="mt-0.5 shrink-0 accent-orange-500 cursor-pointer" />
                        ) : <span className="w-3.5 shrink-0" />}
                        <span className={`flex-1 ${isDone ? "line-through" : ""}`}><span className="font-medium">{pages.join(" + ")}</span>: {inc.issue}</span>
                        {isDone
                          ? <span className="shrink-0 text-emerald-600 text-[10px] whitespace-nowrap">✓ {acked.status} {acked.date}</span>
                          : <button onClick={() => markAcked(action ? action.label : itemText, "noted")} className="shrink-0 text-[10px] text-stone-400 hover:text-stone-600 border border-stone-200 rounded px-1">Mark done</button>
                        }
                        {currentKey === key && <span className="shrink-0 text-orange-500 text-[10px]">merging…</span>}
                      </li>
                    );
                  })}
                </ul>
              </div>
            )}

            {report.missing_connections?.length > 0 && (
              <div>
                <p className="text-xs font-semibold text-stone-600 mb-1.5">🔗 Missing connections <span className="font-normal text-stone-400 ml-1">— add [[wikilinks]] manually, then mark done</span></p>
                <ul className="space-y-1.5">
                  {report.missing_connections.map((c, i) => {
                    const itemText = `${c.from_page}->${c.to_page}:${c.reason}`;
                    const acked = ackedInfo(itemText);
                    const isDone = !!acked;
                    return (
                      <li key={i} className={`text-xs flex items-start gap-2 ${isDone ? "opacity-40" : "text-stone-600"}`}>
                        <span className="w-3.5 shrink-0" />
                        <span className={`flex-1 ${isDone ? "line-through" : ""}`}><span className="font-mono">[[{c.from_page}]]</span> → <span className="font-mono">[[{c.to_page}]]</span>: {c.reason}</span>
                        {isDone
                          ? <span className="shrink-0 text-emerald-600 text-[10px] whitespace-nowrap">✓ {acked.status} {acked.date}</span>
                          : <button onClick={() => markAcked(itemText, "noted")} className="shrink-0 text-[10px] text-stone-400 hover:text-stone-600 border border-stone-200 rounded px-1">Mark done</button>
                        }
                      </li>
                    );
                  })}
                </ul>
              </div>
            )}

            {report.suggested_articles?.length > 0 && (
              <div>
                <p className="text-xs font-semibold text-stone-600 mb-1.5">📝 Suggested articles <span className="font-normal text-stone-400 ml-1">— capture via Capture tab, then mark done</span></p>
                <ul className="space-y-1">
                  {report.suggested_articles.map((a, i) => {
                    const itemText = `article:${a.title}`;
                    const acked = ackedInfo(itemText);
                    const isDone = !!acked;
                    return (
                      <li key={i} className={`text-xs flex items-start gap-2 ${isDone ? "opacity-40" : "text-stone-600"}`}>
                        <span className="w-3.5 shrink-0" />
                        <span className={`flex-1 ${isDone ? "line-through" : ""}`}><span className="font-medium">{a.title}</span>: {a.reason}</span>
                        {isDone
                          ? <span className="shrink-0 text-emerald-600 text-[10px] whitespace-nowrap">✓ {acked.date}</span>
                          : <button onClick={() => markAcked(itemText, "noted")} className="shrink-0 text-[10px] text-stone-400 hover:text-stone-600 border border-stone-200 rounded px-1">Mark done</button>
                        }
                      </li>
                    );
                  })}
                </ul>
              </div>
            )}

            {report.orphaned_pages?.length > 0 && (
              <div>
                <p className="text-xs font-semibold text-stone-600 mb-1.5">🏝 Orphaned pages <span className="font-normal text-stone-400 ml-1">— add [[links]] to them from other pages</span></p>
                <div className="flex flex-wrap gap-1.5">
                  {report.orphaned_pages.map((p, i) => {
                    const itemText = `orphan:${p}`;
                    const acked = ackedInfo(itemText);
                    return acked ? (
                      <span key={i} className="text-[10px] bg-emerald-50 border border-emerald-200 rounded-lg px-2 py-0.5 font-mono text-emerald-600 line-through opacity-50">{p}</span>
                    ) : (
                      <button key={i} onClick={() => markAcked(itemText, "noted")}
                        className="text-xs bg-stone-100 border border-stone-200 rounded-lg px-2 py-0.5 font-mono text-stone-500 hover:bg-orange-50 hover:border-orange-200 hover:text-orange-700 transition-colors"
                        title="Click to mark as acknowledged">
                        {p}
                      </button>
                    );
                  })}
                </div>
              </div>
            )}

            <button onClick={() => { setSaving(true); runLint(true); }} disabled={saving || saved}
              className={`text-xs px-3 py-1.5 rounded-xl border transition-colors ${saved ? "border-emerald-200 text-emerald-600 bg-emerald-50" : "border-stone-200 text-stone-600 hover:bg-stone-50"}`}>
              {saved ? "✓ Saved to wiki" : saving ? "Saving…" : "Save report to wiki"}
            </button>
          </div>
        )}
      </div>

      {/* Sticky batch action footer — only shown when report has actionable items */}
      {actions.length > 0 && (
        <div className="shrink-0 border-t border-stone-100 bg-stone-50/80 px-4 py-2.5 flex items-center justify-between gap-3">
          <button onClick={toggleSelectAll}
            className="text-xs text-stone-500 hover:text-stone-700 underline underline-offset-2 whitespace-nowrap">
            {allSelected ? "Deselect all" : `Select all (${pendingActions.length})`}
          </button>
          <button
            onClick={applySelected}
            disabled={selected.size === 0 || applyingAll}
            className="text-xs px-3 py-1.5 rounded-xl bg-orange-500 text-white font-medium hover:bg-orange-600 disabled:opacity-40 transition-colors whitespace-nowrap">
            {applyingAll
              ? `Applying… (${applyProgress.done}/${applyProgress.total})`
              : selected.size > 0
                ? `Apply selected (${selected.size})`
                : "Apply selected"}
          </button>
        </div>
      )}
    </div>
  );
}

function ConsolidateModal({ pages, prefill, onClose, onDone }) {
  const [source, setSource] = useState(prefill?.source || "");
  const [target, setTarget] = useState(prefill?.target || "");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function handleMerge() {
    if (!source || !target || source === target) return;
    setLoading(true); setError("");
    try {
      await api("/consolidate", { method: "POST", body: JSON.stringify({ source, target }) });
      onDone();
    } catch (e) { setError(e.message); setLoading(false); }
  }

  return (
    <div className="fixed inset-0 bg-black/30 backdrop-blur-sm flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-2xl shadow-xl border border-stone-200 p-5 w-full max-w-sm space-y-4">
        <div className="flex items-center justify-between">
          <h3 className="font-semibold text-stone-900">Merge pages</h3>
          <button onClick={onClose} className="w-6 h-6 flex items-center justify-center text-stone-400 hover:text-stone-600 rounded-lg hover:bg-stone-100 text-xs">✕</button>
        </div>
        <p className="text-xs text-stone-500">Sonnet will merge SOURCE into TARGET, remove duplicates, fix wikilinks, then delete SOURCE.</p>

        <div className="space-y-1.5">
          <label className="text-xs font-medium text-stone-600">Source (will be deleted)</label>
          <select value={source} onChange={e => setSource(e.target.value)}
            className="w-full border border-stone-200 rounded-xl px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-orange-300 bg-white">
            <option value="">— select page —</option>
            {pages.filter(p => p.name !== target).map(p => (
              <option key={p.name} value={p.name}>{p.title}</option>
            ))}
          </select>
        </div>

        <div className="space-y-1.5">
          <label className="text-xs font-medium text-stone-600">Target (kept, merged into)</label>
          <select value={target} onChange={e => setTarget(e.target.value)}
            className="w-full border border-stone-200 rounded-xl px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-orange-300 bg-white">
            <option value="">— select page —</option>
            {pages.filter(p => p.name !== source).map(p => (
              <option key={p.name} value={p.name}>{p.title}</option>
            ))}
          </select>
        </div>

        {error && <p className="text-xs text-red-500">{error}</p>}

        <div className="flex gap-2">
          <button onClick={onClose} className="flex-1 py-2.5 border border-stone-200 rounded-xl text-sm text-stone-600 hover:bg-stone-50 transition-colors">
            Cancel
          </button>
          <button onClick={handleMerge} disabled={!source || !target || source === target || loading}
            className="flex-1 py-2.5 bg-orange-500 text-white rounded-xl text-sm font-medium hover:bg-orange-600 disabled:opacity-40 transition-colors">
            {loading ? "Merging…" : "Merge"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── KNOWLEDGE GRAPH ───────────────────────────────────────────────────────────

const TAG_COLORS_GRAPH = {
  Agents:"#f97316", Agentic:"#fb923c", LLM:"#8b5cf6", RAG:"#06b6d4",
  Inference:"#10b981", KVCache:"#0ea5e9", Attention:"#6366f1",
  Embeddings:"#ec4899", MLOps:"#84cc16", Serving:"#14b8a6",
  Engineering:"#64748b", Systems:"#94a3b8",
};

function GraphPanel({ onClose, onOpenPage }) {
  const canvasRef = useRef(null);
  const [graphData, setGraphData] = useState(null);
  const [hovered, setHovered] = useState(null);
  const simRef = useRef(null);

  useEffect(() => {
    api("/graph").then(d => setGraphData(d)).catch(() => {});
  }, []);

  useEffect(() => {
    if (!graphData || !canvasRef.current) return;
    const canvas = canvasRef.current;
    const W = canvas.width = canvas.offsetWidth;
    const H = canvas.height = canvas.offsetHeight;
    const ctx = canvas.getContext("2d");

    // Init node positions
    const nodes = graphData.nodes.map(n => ({
      ...n,
      x: W / 2 + (Math.random() - 0.5) * W * 0.6,
      y: H / 2 + (Math.random() - 0.5) * H * 0.6,
      vx: 0, vy: 0,
    }));
    const nodeMap = Object.fromEntries(nodes.map(n => [n.id, n]));
    const edges = graphData.edges
      .map(e => ({ source: nodeMap[e.source], target: nodeMap[e.target] }))
      .filter(e => e.source && e.target);

    let frame;
    const REPULSION = 3500, SPRING = 0.04, DAMPING = 0.82, CENTER = 0.012;

    function tick() {
      // Center gravity
      nodes.forEach(n => { n.vx += (W/2 - n.x) * CENTER; n.vy += (H/2 - n.y) * CENTER; });
      // Repulsion
      for (let i = 0; i < nodes.length; i++) for (let j = i+1; j < nodes.length; j++) {
        const dx = nodes[j].x - nodes[i].x, dy = nodes[j].y - nodes[i].y;
        const d2 = Math.max(dx*dx + dy*dy, 100);
        const f = REPULSION / d2;
        nodes[i].vx -= dx*f; nodes[i].vy -= dy*f;
        nodes[j].vx += dx*f; nodes[j].vy += dy*f;
      }
      // Spring edges
      edges.forEach(({ source: s, target: t }) => {
        const dx = t.x - s.x, dy = t.y - s.y;
        const d = Math.sqrt(dx*dx + dy*dy) || 1;
        const f = (d - 120) * SPRING;
        const fx = (dx/d)*f, fy = (dy/d)*f;
        s.vx += fx; s.vy += fy; t.vx -= fx; t.vy -= fy;
      });
      // Integrate
      nodes.forEach(n => {
        n.vx *= DAMPING; n.vy *= DAMPING;
        n.x = Math.max(30, Math.min(W-30, n.x + n.vx));
        n.y = Math.max(30, Math.min(H-30, n.y + n.vy));
      });
    }

    function draw() {
      ctx.clearRect(0, 0, W, H);
      // Edges
      edges.forEach(({ source: s, target: t }) => {
        ctx.beginPath(); ctx.moveTo(s.x, s.y); ctx.lineTo(t.x, t.y);
        ctx.strokeStyle = "rgba(148,163,184,0.35)"; ctx.lineWidth = 1.5; ctx.stroke();
      });
      // Nodes
      nodes.forEach(n => {
        const r = Math.max(8, Math.min(18, 6 + n.entry_count * 2));
        const color = TAG_COLORS_GRAPH[n.tags?.[0]] || "#94a3b8";
        const isHov = hovered?.id === n.id;
        ctx.beginPath(); ctx.arc(n.x, n.y, r + (isHov ? 3 : 0), 0, Math.PI*2);
        ctx.fillStyle = isHov ? color : color + "cc";
        ctx.fill();
        ctx.strokeStyle = "#fff"; ctx.lineWidth = 2; ctx.stroke();
        // Label
        ctx.font = isHov ? "bold 10px system-ui" : "10px system-ui";
        ctx.fillStyle = isHov ? "#1e293b" : "#475569";
        ctx.textAlign = "center";
        const label = n.title.length > 16 ? n.title.slice(0,15)+"…" : n.title;
        ctx.fillText(label, n.x, n.y + r + 12);
      });
    }

    let iter = 0;
    function loop() {
      if (iter++ < 200) tick(); // settle
      draw();
      frame = requestAnimationFrame(loop);
    }
    frame = requestAnimationFrame(loop);
    simRef.current = { nodes, loop };

    // Mouse hover + click
    function getNode(mx, my) {
      return simRef.current?.nodes.find(n => {
        const r = Math.max(8, Math.min(18, 6 + n.entry_count * 2)) + 4;
        return (mx-n.x)**2 + (my-n.y)**2 < r*r;
      });
    }
    function onMove(e) {
      const rect = canvas.getBoundingClientRect();
      const n = getNode(e.clientX - rect.left, e.clientY - rect.top);
      setHovered(n || null);
      canvas.style.cursor = n ? "pointer" : "default";
    }
    function onClick(e) {
      const rect = canvas.getBoundingClientRect();
      const n = getNode(e.clientX - rect.left, e.clientY - rect.top);
      if (n) onOpenPage(n.id);
    }
    canvas.addEventListener("mousemove", onMove);
    canvas.addEventListener("click", onClick);
    return () => { cancelAnimationFrame(frame); canvas.removeEventListener("mousemove", onMove); canvas.removeEventListener("click", onClick); };
  }, [graphData, hovered]);

  return (
    <div className="mb-4 bg-white border border-violet-100 rounded-2xl overflow-hidden shadow-sm">
      <div className="flex items-center justify-between px-4 py-3 bg-violet-50 border-b border-violet-100">
        <span className="text-sm font-semibold text-violet-900">🕸 Knowledge Graph</span>
        <div className="flex items-center gap-3">
          <span className="text-xs text-violet-400">{graphData?.nodes.length ?? 0} pages · {graphData?.edges.length ?? 0} connections</span>
          <button onClick={onClose} className="text-violet-400 hover:text-violet-700 text-lg leading-none">×</button>
        </div>
      </div>
      <div className="relative">
        {!graphData && <div className="h-72 flex items-center justify-center text-sm text-stone-400">Loading graph…</div>}
        {graphData && <canvas ref={canvasRef} className="w-full h-72 block" style={{ height: 288 }} />}
        {hovered && (
          <div className="absolute bottom-3 left-3 bg-white/95 border border-stone-200 rounded-xl px-3 py-2 shadow-sm pointer-events-none">
            <p className="text-xs font-semibold text-stone-800">{hovered.title}</p>
            <p className="text-[10px] text-stone-400">{hovered.entry_count} source{hovered.entry_count !== 1 ? "s" : ""} · {hovered.tags?.join(", ")}</p>
          </div>
        )}
      </div>
    </div>
  );
}

// ── INSIGHTS PANEL ────────────────────────────────────────────────────────────

function InsightsPanel({ onClose }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [analyzing, setAnalyzing] = useState(false);
  const [error, setError] = useState("");

  async function load() {
    setLoading(true);
    try {
      const d = await api("/system-insights");
      setData(d);
    } catch (e) { setError(e.message); }
    finally { setLoading(false); }
  }

  useEffect(() => { load(); }, []);

  async function runAnalysis() {
    setAnalyzing(true); setError("");
    try {
      const d = await api("/analyze-traces", { method: "POST" });
      await load();
    } catch (e) { setError(e.message); }
    finally { setAnalyzing(false); }
  }

  const SECTION_ICONS = {
    "Extraction Patterns": "📊",
    "Tag Confusion": "🏷️",
    "Duplicate Signals": "🔁",
    "Rejection Patterns": "🚫",
    "Prompt Hints": "💡",
    "Routing Recommendations": "🔀",
    "Architecture Recommendations": "🏗️",
  };

  return (
    <div className="mb-4 bg-white border border-indigo-100 rounded-2xl overflow-hidden shadow-sm">
      <div className="flex items-center justify-between px-4 py-3 bg-indigo-50 border-b border-indigo-100">
        <div>
          <span className="text-sm font-semibold text-indigo-900">🧠 System Learnings</span>
          {data?.last_analyzed && (
            <span className="ml-2 text-xs text-indigo-400">Last analyzed {data.last_analyzed}</span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-indigo-400">{data?.traces_count ?? 0} traces</span>
          <button onClick={runAnalysis} disabled={analyzing || (data?.traces_count ?? 0) < 3}
            className="text-xs px-3 py-1 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-40 transition-colors">
            {analyzing ? "Analyzing…" : "Run analysis"}
          </button>
          <button onClick={onClose} className="text-indigo-400 hover:text-indigo-700 text-lg leading-none">×</button>
        </div>
      </div>

      <div className="p-4">
        {loading && <p className="text-sm text-stone-400 text-center py-4">Loading…</p>}
        {error && <p className="text-xs text-red-500 mb-3">{error}</p>}

        {!loading && !data?.exists && (
          <div className="text-center py-6 space-y-2">
            <p className="text-sm text-stone-500">No analysis yet.</p>
            <p className="text-xs text-stone-400">
              {(data?.traces_count ?? 0) < 3
                ? `Need at least 3 traces — approve ${3 - (data?.traces_count ?? 0)} more item${3 - (data?.traces_count ?? 0) !== 1 ? "s" : ""} first`
                : "Click Run analysis to generate insights from your traces"}
            </p>
          </div>
        )}

        {!loading && data?.exists && data.sections && (
          <div className="space-y-4">
            {Object.entries(data.sections)
              .filter(([, items]) => items.length > 0)
              .map(([section, items]) => (
                <div key={section}>
                  <p className="text-xs font-semibold text-stone-500 uppercase tracking-wider mb-1.5">
                    {SECTION_ICONS[section] || "•"} {section}
                  </p>
                  <ul className="space-y-1">
                    {items.map((item, i) => (
                      <li key={i} className={`text-xs rounded-lg px-3 py-2 ${
                        section === "Prompt Hints" ? "bg-amber-50 text-amber-800 border border-amber-100" :
                        section === "Architecture Recommendations" ? "bg-indigo-50 text-indigo-800 border border-indigo-100" :
                        "bg-stone-50 text-stone-700"
                      }`}>
                        {item}
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
          </div>
        )}
      </div>
    </div>
  );
}

const FOLDERS = [
  { key: "concepts", label: "Concepts" },
  { key: "sources", label: "Sources" },
  { key: "insights", label: "Insights" },
];

function BrowseTab() {
  const [folder, setFolder] = useState("concepts");
  const [pages, setPages] = useState([]);
  const [search, setSearch] = useState("");
  const [deepDiveFilter, setDeepDiveFilter] = useState(false);
  const [selected, setSelected] = useState(null);
  const [pageContent, setPageContent] = useState("");
  const [pageLoading, setPageLoading] = useState(false);
  const [showLint, setShowLint] = useState(false);
  const [showInsights, setShowInsights] = useState(false);
  const [showGraph, setShowGraph] = useState(false);
  const [reviewFilter, setReviewFilter] = useState(false);
  const [reviewPages, setReviewPages] = useState([]);
  const [consolidateModal, setConsolidateModal] = useState(null);
  const [fixing, setFixing] = useState({});
  const [deleting, setDeleting] = useState(null);

  function reloadPages(f = folder) {
    api(`/pages?folder=${f}`).then(d => setPages(d.pages)).catch(() => {});
  }

  function switchFolder(f) {
    setFolder(f); setSelected(null); setSearch(""); setDeepDiveFilter(false); setReviewFilter(false); reloadPages(f);
  }

  async function toggleReview() {
    if (reviewFilter) { setReviewFilter(false); return; }
    const d = await api("/review-queue?days=30").catch(() => ({ pages: [] }));
    setReviewPages(d.pages || []);
    setReviewFilter(true);
  }

  useEffect(() => {
    // Retry on mount — keeps retrying every 2s until pages load (handles slow app startup)
    let attempts = 0;
    const tryLoad = () => {
      api(`/pages?folder=${folder}`)
        .then(d => { if (d.pages?.length > 0) setPages(d.pages); else if (attempts++ < 15) setTimeout(tryLoad, 2000); })
        .catch(() => { if (attempts++ < 15) setTimeout(tryLoad, 2000); });
    };
    tryLoad();
  }, [folder]);

  useEffect(() => {
    // Poll every 10s to pick up newly approved items
    const interval = setInterval(() => {
      api(`/pages?folder=${folder}`)
        .then(d => { if (d.pages) setPages(d.pages); })
        .catch(() => {});
    }, 10000);
    return () => clearInterval(interval);
  }, [folder]);

  const [parsedPage, setParsedPage] = useState(null);

  async function openPage(name) {
    setPageLoading(true);
    // Track reading history in localStorage (last 8 pages)
    const history = JSON.parse(localStorage.getItem("sw_read_history") || "[]")
      .filter(n => n !== name).slice(0, 7);
    history.unshift(name);
    localStorage.setItem("sw_read_history", JSON.stringify(history));

    try {
      const data = await api(`/page/${name}`);
      setPageContent(data.content);
      const parsed = data.parsed ? { ...data.parsed, backlinks: data.backlinks || [], pageName: name } : null;
      setParsedPage(parsed);
      setSelected(name);
    } catch { setPageContent("Failed to load page."); setParsedPage(null); setSelected(name); }
    finally { setPageLoading(false); }
  }

  async function handleDelete(name, e) {
    e.stopPropagation();
    if (!window.confirm(`Delete [[${name}]] permanently?`)) return;
    setDeleting(name);
    try { await api(`/page/${name}`, { method: "DELETE" }); reloadPages(); }
    catch (err) { alert(`Delete failed: ${err.message}`); }
    finally { setDeleting(null); }
  }

  async function handleFix(name) {
    setFixing(p => ({ ...p, [name]: true }));
    try {
      const r = await api(`/fix-page/${name}`, { method: "POST" });
      alert(`Fixed [[${name}]]: ${r.wikilinks_fixed} wikilinks normalised, ${r.entry_count_updated} entries.`);
      reloadPages();
    } catch (e) { alert(`Fix failed: ${e.message}`); }
    finally { setFixing(p => ({ ...p, [name]: false })); }
  }

  const reviewPageNames = new Set(reviewPages.map(p => p.name));
  const filtered = (reviewFilter ? reviewPages : pages)
    .filter(p => !(folder === "insights" && p.name.includes("lint-report")))
    .filter(p => !deepDiveFilter || (p.tags || []).some(t => t.toLowerCase() === "deep-dive"))
    .filter(p => {
      const q = search.toLowerCase();
      return !q || p.title.toLowerCase().includes(q) || p.name.toLowerCase().includes(q) ||
        (p.tags || []).some(t => t.toLowerCase().includes(q));
    });

  function parseContentChunks(md) {
    const parts = md.split(/```mermaid\n?([\s\S]*?)```/g);
    const chunks = [];
    for (let i = 0; i < parts.length; i++) {
      if (i % 2 === 0) { if (parts[i].trim()) chunks.push({ type: "md", content: parts[i] }); }
      else chunks.push({ type: "mermaid", content: parts[i].trim() });
    }
    return chunks;
  }

  function renderMd(md) {
    return { __html: marked.parse(md) };
  }

  if (selected) {
    return (
      <div className="h-full flex flex-col">
        {/* Header */}
        <div className="flex items-center gap-2 mb-4 pb-3 border-b border-stone-100 shrink-0">
          <button onClick={() => { setSelected(null); setParsedPage(null); }}
            className="flex items-center gap-1.5 text-sm text-stone-500 hover:text-stone-800 transition-colors">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7"/></svg>
            Back
          </button>
          <span className="text-stone-300 mx-1">/</span>
          <span className="text-sm font-medium text-stone-700 flex-1 truncate">{selected}</span>
          <div className="flex items-center gap-1">
            {folder === "concepts" && (
              <button onClick={() => handleFix(selected)} disabled={fixing[selected]}
                className="text-xs px-2.5 py-1 border border-stone-200 text-stone-500 rounded-lg hover:bg-stone-50 disabled:opacity-40 transition-colors">
                {fixing[selected] ? "…" : "Fix"}
              </button>
            )}
            <button onClick={() => {
              handleDelete(selected, { stopPropagation: () => {} });
              setSelected(null);
            }} className="text-xs px-2.5 py-1 border border-red-100 text-red-400 rounded-lg hover:bg-red-50 transition-colors">
              Delete
            </button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto">
          {pageLoading ? (
            <div className="flex items-center gap-2 text-sm text-stone-400 pt-8 justify-center">
              <svg className="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"/><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg>
              Loading…
            </div>
          ) : parsedPage && folder === "concepts" ? (
            <ConceptPageView page={{ ...parsedPage, _onOpenPage: openPage }} />
          ) : (
            <div className="space-y-3">
              {parseContentChunks(pageContent).map((chunk, i) =>
                chunk.type === "mermaid"
                  ? <MermaidDiagram key={i} chart={chunk.content} />
                  : <div key={i} className="prose prose-sm max-w-none text-stone-800 prose-headings:text-stone-900 prose-a:text-orange-600 prose-code:text-orange-600 prose-code:bg-orange-50 prose-code:rounded prose-code:px-1"
                      dangerouslySetInnerHTML={{ __html: marked.parse(chunk.content) }} />
              )}
            </div>
          )}
        </div>

        {consolidateModal !== null && (
          <ConsolidateModal pages={pages} prefill={consolidateModal}
            onClose={() => setConsolidateModal(null)}
            onDone={() => { setConsolidateModal(null); setSelected(null); reloadPages(); }} />
        )}
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <div>
          <h2 className="text-base font-semibold text-stone-900">Knowledge Base</h2>
          <p className="text-xs text-stone-400 mt-0.5">{filtered.length} page{filtered.length !== 1 ? "s" : ""}{deepDiveFilter ? " flagged for deeper research" : reviewFilter ? " not reviewed in 30+ days" : ` in ${folder}`}</p>
        </div>
        <div className="flex gap-1.5">
          {folder === "concepts" && (
            <>
              <button onClick={() => setDeepDiveFilter(v => !v)}
                className={`text-xs px-3 py-1.5 rounded-xl border transition-colors ${deepDiveFilter ? "bg-orange-500 text-white border-orange-500" : "border-orange-200 text-orange-500 hover:bg-orange-50"}`}>
                🔍 Want more
              </button>
              <button onClick={toggleReview}
                className={`text-xs px-3 py-1.5 rounded-xl border transition-colors ${reviewFilter ? "bg-amber-500 text-white border-amber-500" : "border-amber-200 text-amber-600 hover:bg-amber-50"}`}>
                🕰 Review
              </button>
              <button onClick={() => { setShowGraph(v => !v); setShowInsights(false); setShowLint(false); }}
                className={`text-xs px-3 py-1.5 rounded-xl border transition-colors ${showGraph ? "bg-violet-600 text-white border-violet-600" : "border-violet-200 text-violet-600 hover:bg-violet-50"}`}>
                🕸 Graph
              </button>
            </>
          )}
          <button onClick={() => { setShowInsights(v => !v); setShowLint(false); setShowGraph(false); }}
            className={`text-xs px-3 py-1.5 rounded-xl border transition-colors ${showInsights ? "bg-indigo-600 text-white border-indigo-600" : "border-indigo-200 text-indigo-500 hover:bg-indigo-50"}`}>
            🧠 Learn
          </button>
          <button onClick={() => { setShowLint(v => !v); setShowInsights(false); setShowGraph(false); }}
            className={`text-xs px-3 py-1.5 rounded-xl border transition-colors ${showLint ? "bg-stone-900 text-white border-stone-900" : "border-stone-200 text-stone-500 hover:bg-stone-50"}`}>
            Health
          </button>
        </div>
      </div>

      {showInsights && (
        <InsightsPanel onClose={() => setShowInsights(false)} />
      )}

      {showGraph && (
        <GraphPanel onClose={() => setShowGraph(false)} onOpenPage={name => { setShowGraph(false); openPage(name); }} />
      )}

      {showLint && (
        <LintPanel onClose={() => setShowLint(false)}
          onConsolidate={(s, t) => setConsolidateModal({ source: s, target: t })}
          onFix={name => handleFix(name)} />
      )}

      {consolidateModal !== null && (
        <ConsolidateModal pages={pages} prefill={consolidateModal}
          onClose={() => setConsolidateModal(null)}
          onDone={() => { setConsolidateModal(null); reloadPages(); }} />
      )}

      {/* Folder tabs */}
      <div className="flex gap-1 mb-3 bg-stone-100 rounded-xl p-1">
        {FOLDERS.map(f => (
          <button key={f.key} onClick={() => switchFolder(f.key)}
            className={`flex-1 text-xs py-1.5 rounded-lg font-medium transition-colors ${
              folder === f.key ? "bg-white text-stone-800 shadow-sm" : "text-stone-500 hover:text-stone-700"
            }`}>
            {f.label}
          </button>
        ))}
      </div>

      {/* Search */}
      <div className="relative mb-3">
        <svg className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-stone-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/></svg>
        <input
          className="w-full pl-9 pr-4 py-2 bg-white border border-stone-200 rounded-xl text-sm placeholder-stone-400 focus:outline-none focus:ring-2 focus:ring-orange-200 focus:border-orange-300"
          placeholder={`Search ${folder}…`}
          value={search}
          onChange={e => setSearch(e.target.value)}
        />
      </div>

      {/* Pages list */}
      <div className="flex-1 overflow-y-auto space-y-2">
        {filtered.length === 0 && (
          <div className="flex flex-col items-center justify-center pt-12 space-y-3">
            <div className="w-10 h-10 bg-stone-100 rounded-2xl flex items-center justify-center">
              <svg className="w-5 h-5 text-stone-300" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>
            </div>
            <p className="text-sm text-stone-400">
              {pages.length === 0 ? `No pages in ${folder} yet` : "No matches"}
            </p>
            {pages.length === 0 && (
              <button onClick={() => reloadPages()} className="text-xs text-orange-500 hover:text-orange-600">
                Reload
              </button>
            )}
          </div>
        )}

        {filtered.map(page => (
          <div key={page.name}
            className="group bg-white border border-stone-200 rounded-2xl p-4 hover:border-stone-300 hover:shadow-sm transition-all cursor-pointer"
            onClick={() => openPage(page.name)}>
            <div className="flex items-start justify-between gap-2">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-1.5">
                  <span className="font-medium text-stone-900 text-sm truncate">{page.title}</span>
                  {page.entry_count > 0 && (
                    <span className="shrink-0 text-xs text-stone-400 bg-stone-100 px-1.5 py-0.5 rounded-full">
                      {page.entry_count}
                    </span>
                  )}
                </div>
                {page.tags?.length > 0 && (
                  <div className="flex flex-wrap gap-1">
                    {page.tags.map(t => <TagPill key={t} tag={t} />)}
                  </div>
                )}
                {page.last_updated && (
                  <p className="text-xs text-stone-400 mt-1.5">
                    {page.last_updated}
                    {reviewFilter && page.days_since_update && (
                      <span className="ml-2 text-amber-500 font-medium">{page.days_since_update}d ago</span>
                    )}
                  </p>
                )}
              </div>

              <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity shrink-0"
                onClick={e => e.stopPropagation()}>
                {folder === "concepts" && (
                  <button onClick={() => handleFix(page.name)} disabled={fixing[page.name]}
                    title="Fix wikilinks" className="w-7 h-7 flex items-center justify-center text-stone-400 hover:text-stone-700 hover:bg-stone-100 rounded-lg text-xs transition-colors">
                    {fixing[page.name] ? "…" : "✦"}
                  </button>
                )}
                <button
                  onClick={e => handleDelete(page.name, e)}
                  disabled={deleting === page.name}
                  title="Delete" className="w-7 h-7 flex items-center justify-center text-stone-400 hover:text-red-500 hover:bg-red-50 rounded-lg text-xs transition-colors">
                  {deleting === page.name ? "…" : "×"}
                </button>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── APP SHELL ─────────────────────────────────────────────────────────────────

const TABS = [
  { id: "ingest", label: "Capture", icon: (
    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M12 4v16m8-8H4"/></svg>
  )},
  { id: "chat", label: "Chat", icon: (
    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z"/></svg>
  )},
  { id: "browse", label: "Browse", icon: (
    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"/></svg>
  )},
];

function BackendStatus() {
  const [online, setOnline] = useState(null); // null=checking, true, false

  useEffect(() => {
    function check() {
      fetch(`${API}/queue`).then(() => setOnline(true)).catch(() => setOnline(false));
    }
    check();
    const interval = setInterval(check, 5000);
    return () => clearInterval(interval);
  }, []);

  if (online === null) return null;
  return (
    <div className={`flex items-center gap-1.5 text-xs ${online ? "text-emerald-600" : "text-red-500"}`}>
      <span className={`w-1.5 h-1.5 rounded-full ${online ? "bg-emerald-400" : "bg-red-400 animate-pulse"}`} />
      {online ? "Connected" : "Backend offline"}
    </div>
  );
}

export default function App() {
  const [tab, setTab] = useState("ingest");
  const [tagGroups, setTagGroups] = useState({});

  useEffect(() => { fetchTagGroups().then(setTagGroups); }, []);

  function refreshTagGroups() {
    fetchTagGroups().then(setTagGroups);
  }

  return (
    <TagGroupsContext.Provider value={tagGroups}>
    <div className="min-h-screen bg-stone-50 flex flex-col font-sans">
      {/* Header */}
      <header className="bg-white border-b border-stone-200 px-4 py-3 flex items-center justify-between sticky top-0 z-40">
        <div className="flex items-center gap-2.5">
          <div className="w-7 h-7 bg-orange-500 rounded-lg flex items-center justify-center shrink-0">
            <svg className="w-4 h-4 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z"/></svg>
          </div>
          <div>
            <span className="text-sm font-semibold text-stone-900">SakethWiki</span>
            <span className="text-stone-400 text-xs ml-1.5">knowledge base</span>
          </div>
        </div>
        <BackendStatus />
      </header>

      {/* Tab bar */}
      <div className="bg-white border-b border-stone-200 px-4">
        <div className="flex max-w-2xl mx-auto w-full">
          {TABS.map((t) => (
            <button key={t.id} onClick={() => setTab(t.id)}
              className={`flex items-center gap-2 px-4 py-3 text-sm font-medium border-b-2 transition-colors ${
                tab === t.id
                  ? "border-orange-500 text-orange-600"
                  : "border-transparent text-stone-500 hover:text-stone-700"
              }`}>
              {t.icon}
              {t.label}
            </button>
          ))}
        </div>
      </div>

      {/* Main content */}
      <main className="flex-1 flex flex-col overflow-hidden">
        <div className="flex-1 overflow-y-auto p-4 max-w-2xl mx-auto w-full">
          {tab === "ingest" && <IngestTab onApproved={refreshTagGroups} onSwitchToChat={() => setTab("chat")} />}
          {tab === "chat" && (
            <div className="flex flex-col" style={{ height: "calc(100vh - 120px)" }}>
              <ChatTab />
            </div>
          )}
          {tab === "browse" && (
            <div className="flex flex-col" style={{ height: "calc(100vh - 120px)" }}>
              <BrowseTab />
            </div>
          )}
        </div>
      </main>
    </div>
    </TagGroupsContext.Provider>
  );
}
