#!/usr/bin/env python3
"""build-chunks.py - split the xAPI/cmi5 reference into retrieval-ready JSONL chunks.

Where to run: from the package root (the folder containing xapi-cmi5-reference.md). Python 3.8+, stdlib only.
Usage:  python3 scripts/build-chunks.py [--max-chars 5000] [--out corpus/xapi-cmi5-chunks.jsonl]
Each chunk = one H2 or H3 section (oversized sections split on paragraph boundaries, never inside code
fences or tables, so a single large table can exceed --max-chars), prefixed with a breadcrumb so it stands alone. Example files become one chunk each.
Fields: id, doc_id, source_file, section, subsection, tag, part, kind, text, n_chars, approx_tokens, compiled.
Re-run after editing the reference; output is deterministic.
"""
import argparse, json, pathlib, re

ap = argparse.ArgumentParser()
ap.add_argument("--src", default="xapi-cmi5-reference.md")
ap.add_argument("--examples", default="examples")
ap.add_argument("--out", default="corpus/xapi-cmi5-chunks.jsonl")
ap.add_argument("--max-chars", type=int, default=5000)
a = ap.parse_args()

raw = pathlib.Path(a.src).read_text(encoding="utf-8")
meta, body = {}, raw
if raw.startswith("---\n"):
    fm, body = raw[4:].split("\n---\n", 1)
    for line in fm.splitlines():
        m = re.match(r"^(\w+):\s*(.*)$", line)
        if m and m.group(2) and not m.group(2).startswith(">"):
            meta[m.group(1)] = m.group(2).strip().strip('"')
DOC, TITLE, COMPILED = meta.get("doc_id", "doc"), meta.get("title", "Reference"), meta.get("compiled", "")

def slug(s): return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]

# walk lines, tracking fences; start a new section at H2/H3 outside fences
sections, cur, h2, h3, fence = [], None, "", "", False
for line in body.splitlines():
    if line.lstrip().startswith("```"):
        fence = not fence
    if not fence and re.match(r"^#{2,3} ", line):
        if cur: sections.append(cur)
        level, title = len(line.split(" ")[0]), line.split(" ", 1)[1].strip()
        if level == 2: h2, h3 = title, ""
        else: h3 = title
        cur = {"section": h2, "subsection": h3, "lines": [line]}
    elif cur is not None:
        cur["lines"].append(line)
if cur: sections.append(cur)

def blocks(lines):  # paragraph blocks, keeping fenced code intact
    out, buf, inf = [], [], False
    for ln in lines:
        if ln.lstrip().startswith("```"): inf = not inf
        if not inf and ln.strip() == "" and buf:
            out.append("\n".join(buf)); buf = []
        else:
            buf.append(ln)
    if buf: out.append("\n".join(buf))
    return out

chunks, h2tag, lasth2 = [], "", None
for s in sections:
    if s["section"] != lasth2:
        lasth2, h2tag = s["section"], ""
    text = "\n".join(s["lines"]).strip()
    if not s["subsection"]:
        t0 = next((m.group(1) for m in re.finditer(r"^Tag:\s*(\S+)", text, re.M)), "")
        if t0: h2tag = t0
    if len(text.splitlines()) <= 1:       # heading only (e.g. H2 that immediately opens H3s)
        continue
    tag = next((m.group(1) for m in re.finditer(r"^Tag:\s*(\S+)", text, re.M)), "")
    if not s["subsection"] and tag: h2tag = tag
    tag = tag or h2tag                    # H3 without its own Tag inherits the H2 tag
    crumb = f"[{TITLE} | {s['section']}" + (f" > {s['subsection']}" if s["subsection"] else "") + "]"
    parts, buf = [], ""
    for b in blocks(s["lines"]):
        if buf and len(buf) + len(b) + 2 > a.max_chars:
            parts.append(buf); buf = b
        else:
            buf = f"{buf}\n\n{b}" if buf else b
    if buf: parts.append(buf)
    for i, p in enumerate(parts, 1):
        t = f"{crumb}\n\n{p.strip()}" if i == 1 else f"{crumb} (part {i}/{len(parts)})\n\n{p.strip()}"
        base = slug(s["subsection"] or s["section"])
        chunks.append({"id": f"{DOC}#{base}" + (f"-p{i}" if len(parts) > 1 else ""), "doc_id": DOC,
                       "source_file": a.src, "section": s["section"], "subsection": s["subsection"], "tag": tag,
                       "part": i, "kind": "reference", "text": t})

# examples: one chunk per file, with the description from examples/README.md when available
ex = pathlib.Path(a.examples); desc = {}
readme = ex / "README.md"
if readme.exists():
    for m in re.finditer(r"^\|\s*`([^`]+)`\s*\|\s*(.+?)\s*\|\s*$", readme.read_text(encoding="utf-8"), re.M):
        desc[m.group(1)] = m.group(2)
for f in sorted(ex.glob("*")):
    if f.name == "README.md" or not f.is_file(): continue
    lang = {"json": "json", "jsonld": "json", "xml": "xml"}.get(f.suffix.lstrip("."), "")
    t = (f"[{TITLE} | Example file {f.name}]\n\n{desc.get(f.name, '')}\n\n"
         f"```{lang}\n{f.read_text(encoding='utf-8').strip()}\n```")
    chunks.append({"id": f"{DOC}#example-{slug(f.stem)}", "doc_id": DOC, "source_file": f"{a.examples}/{f.name}",
                   "section": "Examples", "subsection": f.name, "tag": "EXAMPLE", "part": 1, "kind": "example", "text": t})

seen = set()
for c in chunks:
    while c["id"] in seen: c["id"] += "-dup"
    seen.add(c["id"])
    c["n_chars"] = len(c["text"]); c["approx_tokens"] = round(len(c["text"]) / 4); c["compiled"] = COMPILED

out = pathlib.Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
with out.open("w", encoding="utf-8") as fh:
    for c in chunks: fh.write(json.dumps(c, ensure_ascii=False) + "\n")
print(f"wrote {len(chunks)} chunks to {out} "
      f"(max {max(c['n_chars'] for c in chunks)} chars, total ~{sum(c['approx_tokens'] for c in chunks)} tokens)")
