#!/usr/bin/env bash
# Render the role guides (docs/guides/*.md) to PDF with pandoc, for printing or a transfer kit.
#
#   scripts/build_guides_pdf.sh             # writes build/guides-pdf/<guide>.pdf
#   scripts/build_guides_pdf.sh <out-dir>   # writes there instead
#
# Needs pandoc and a PDF engine (default xelatex; PDF_ENGINE=<engine> to choose another).
# Ubuntu: sudo apt-get install -y pandoc texlive-xetex texlive-fonts-recommended
# macOS:  brew install pandoc, plus a TeX distribution (e.g. BasicTeX).
# Without them it says what is missing and exits 2; it never half-builds a set.
# Not part of release.yml: run it when the documentation set is cut (docs/guides/README.md).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GUIDES="$ROOT/docs/guides"
OUT="${1:-$ROOT/build/guides-pdf}"
ENGINE="${PDF_ENGINE:-xelatex}"

if ! command -v pandoc >/dev/null 2>&1; then
  echo "build_guides_pdf: pandoc is not installed (see the header of $0)" >&2
  exit 2
fi
if ! command -v "$ENGINE" >/dev/null 2>&1; then
  echo "build_guides_pdf: PDF engine '$ENGINE' is not installed (set PDF_ENGINE, or see the header of $0)" >&2
  exit 2
fi

shopt -s nullglob
guides=("$GUIDES"/[0-9][0-9]-*.md "$GUIDES"/README.md)
if [ "${#guides[@]}" -eq 0 ]; then
  echo "build_guides_pdf: no guides under $GUIDES" >&2
  exit 1
fi

mkdir -p "$OUT"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# Wrap long lines in code blocks instead of running them off the page.
cat >"$work/header.tex" <<'EOF'
\usepackage{fvextra}
\DefineVerbatimEnvironment{Highlighting}{Verbatim}{breaklines,commandchars=\\\{\}}
EOF

# Fonts with the box-drawing and arrow characters the guides use, when the system has them.
fontopts=()
if command -v fc-list >/dev/null 2>&1 && fc-list 2>/dev/null | grep -q "DejaVu Sans Mono"; then
  fontopts=(-V "mainfont=DejaVu Sans" -V "monofont=DejaVu Sans Mono")
fi

for md in "${guides[@]}"; do
  name="$(basename "$md" .md)"
  [ "$name" = "README" ] && name="00-index"
  pandoc "$md" --from gfm --pdf-engine="$ENGINE" \
    --resource-path="$GUIDES" --include-in-header="$work/header.tex" \
    -V geometry:margin=2cm -V fontsize=10pt -V colorlinks=true \
    ${fontopts[@]+"${fontopts[@]}"} \
    -o "$OUT/$name.pdf"
  echo "wrote $OUT/$name.pdf"
done
