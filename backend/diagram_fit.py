"""
Make Mermaid diagrams narrower so they stay readable in the app and in Obsidian.

fit() is a pure function: text in, text out. It repairs one syntax slip, switches
long left-to-right flowcharts to top-to-bottom, and wraps long node labels. It never
adds, removes or renames a node or an edge. See docs/diagram-fit/.

Thresholds were measured on the 208 diagrams in the vault (headless Mermaid 11.14,
700 px pane): see docs/diagram-fit/adr.md ADR 2 and ADR 3.
"""
import logging
import re

logger = logging.getLogger(__name__)

MIN_NODES_FOR_TD = 4   # fewer nodes: the diagram is short in either direction
MAX_FANOUT_FOR_TD = 2  # a node with 3+ outgoing edges puts its children side by side in TD: wider, not narrower
WRAP_TRIGGER = 28      # node labels longer than this get line breaks
WRAP_AT = 24           # target line length when wrapping

_FLOW = re.compile(r"^(\s*)(flowchart|graph)(\s+)(LR|RL|TD|TB|BT)\b", re.I)
_NOT_IDS = {"flowchart", "graph", "subgraph", "classDef", "class", "style", "linkStyle", "click", "end",
            "direction", "LR", "RL", "TD", "TB", "BT"}


def repair(src: str) -> str:
    """Remove the space before `:::class` (`D["x"] :::accent` and `A :::accent` are rejected by Mermaid)."""
    return re.sub(r'([\]\)\}"\w])[ \t]+:::', r"\1:::", src)


def _header_index(lines: list[str]) -> int:
    """Index of the first diagram line: skips blank lines, %% comments and --- front matter."""
    i = 0
    if i < len(lines) and lines[i].strip() == "---":
        i += 1
        while i < len(lines) and lines[i].strip() != "---":
            i += 1
        i += 1
    while i < len(lines) and (not lines[i].strip() or lines[i].strip().startswith("%%")):
        i += 1
    return i


def _node_count(src: str) -> int:
    body = re.sub(r'"[^"]*"', "", src)
    ids = set(re.findall(r"\b([A-Za-z_]\w*)\s*(?:\[|\(|\{|-->|---|-\.|==|\|)", body)) - _NOT_IDS
    return len(ids)


def _max_fanout(src: str) -> int:
    """Largest number of edges leaving one node."""
    body = re.sub(r'"[^"]*"', "", src)
    body = re.sub(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}", "", body)
    out: dict[str, int] = {}
    for line in body.split("\n"):
        if line.strip().startswith(("classDef", "class ", "style", "subgraph", "end", "flowchart", "graph", "linkStyle", "%%")):
            continue
        parts = re.split(r"\s*(?:-->|---|-\.->|-\.-|==>|--)\s*(?:\|[^|]*\|)?\s*", line)
        ids = [re.sub(r"\W", "", p.split("&")[0]) for p in parts]
        for a, b in zip(ids, ids[1:]):
            if a and b:
                out[a] = out.get(a, 0) + 1
    return max(out.values(), default=0)


def _wrap(label: str) -> str:
    if "<br" in label or len(label) <= WRAP_TRIGGER:
        return label
    lines: list[str] = []
    cur = ""
    for word in label.split(" "):
        if cur and len(cur) + 1 + len(word) > WRAP_AT:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        lines.append(cur)
    return "<br/>".join(lines)


def _wrap_labels(src: str) -> str:
    return re.sub(r'(\[\s*")([^"\]]+)("\s*\])', lambda m: m.group(1) + _wrap(m.group(2)) + m.group(3), src)


def fit(src: str) -> str:
    """Return a narrower, repaired version of a diagram. Never raises."""
    try:
        if not src or not src.strip():
            return src
        out = repair(src)
        lines = out.split("\n")
        idx = _header_index(lines)
        head = _FLOW.match(lines[idx]) if idx < len(lines) else None
        if not head:
            return out  # mind map, sequence diagram and so on: syntax repair only
        if head.group(4).upper() in ("LR", "RL") and not re.search(r"^\s*subgraph\b", out, re.M) \
                and _node_count(out) >= MIN_NODES_FOR_TD and _max_fanout(out) <= MAX_FANOUT_FOR_TD:
            lines[idx] = _FLOW.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}TD", lines[idx], count=1)
            out = "\n".join(lines)
        out = _wrap_labels(out)
        if not out.strip() or len(out.split("\n")) != len(src.split("\n")):
            return src
        return out
    except Exception as e:  # never block a capture on cosmetics
        logger.warning("diagram_fit failed: %s", e)
        return src
