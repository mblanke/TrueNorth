# Author: Marc-Andre Blanke
# Created: 2026-11-18T17:30:00Z
"""Every source file opens with an authorship header (last-mile 1.10).

A new .py, .ts or .sh file needs `# Author: ...` and `# Created: <UTC>` lines at its top.
Add them with `python scripts/authorship_headers.py --fix` (it stamps the first commit's time,
or now for a file not yet committed).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/authorship_headers.py"


def test_every_source_file_has_an_authorship_header():
    run = subprocess.run([sys.executable, str(SCRIPT), "--check"], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stdout[-2000:] + run.stderr[-500:]


def test_the_header_goes_after_a_shebang_and_before_a_docstring():
    sys.path.insert(0, str(ROOT / "scripts"))
    import authorship_headers as ah

    py = ah.with_header('"""Doc."""\nx = 1\n', "a.py", "2026-01-02T03:04:05Z")
    assert py.startswith("# Author: Marc-Andre Blanke\n# Created: 2026-01-02T03:04:05Z\n\"\"\"Doc.\"\"\"")
    sh = ah.with_header("#!/usr/bin/env bash\nset -e\n", "a.sh", "2026-01-02T03:04:05Z")
    assert sh.splitlines()[0] == "#!/usr/bin/env bash" and sh.splitlines()[1] == "# Author: Marc-Andre Blanke"
    ts = ah.with_header("export const a = 1;\n", "a.ts", "2026-01-02T03:04:05Z")
    assert ts.startswith("// Author: Marc-Andre Blanke\n// Created: 2026-01-02T03:04:05Z\nexport")
    assert ah.has_header(py) and ah.has_header(sh) and ah.has_header(ts) and not ah.has_header("x = 1\n")
