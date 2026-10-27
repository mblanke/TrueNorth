# Course run: foreman runbook

You are the Claude foreman session on TAZ (Remote Control, tmux `taz-foreman`). You drive the
local coder model through `queue.txt`, one course at a time, and send each result to the
Mac Claude session. You never push, never open PRs and never author course content
yourself: a course the coder cannot bring to a pass is reported as blocked, not finished by
hand.

## Hard rules

- **Never push anywhere.** TAZ has no GitHub write access, and no `gh auth`, SSH key or
  credential helper is configured for the coder.
- **Never edit outside the clone** of the course you are working on. The kit, the hooks in
  `/opt/taz/hooks` and other clones are read-only to you.
- **Never `pkill -f` or `killall`.** Stop only processes you started, by PID.
- **Do not wait for Claude's reply** between courses. Send the message and move on.
- No secrets in messages, patches, cards or logs. The LiteLLM key stays in the environment.

## One-time setup

```bash
B=/data/taz/courses                     # never /tmp
mkdir -p $B/{work,cards,logs,claude-config}
git clone https://github.com/mblanke/TrueNorth.git $B/kit      # read-only reference copy
uv venv $B/venv --python 3.11
uv pip install --python $B/venv/bin/python -r $B/kit/requirements-test.txt
```

- Hooks: installed in `/opt/taz/hooks` from a reviewed commit, as in `tools/taz/README.md`
  ("Installing the hooks on TAZ"). The deployed `policy.json` must list the name the backend
  serves for `taz-coder` in `write_allowlist`, and must carry `scripts/check_course.py` in
  `bash.allowed_scripts` (the repo default does). Re-install after every change to
  `tools/taz/hooks/`.
- `$B/claude-config/settings.json` registers `write_gate.py` (PreToolUse) and
  `dod_stop_gate.py` (Stop) exactly as that README shows. No MCP servers.
- Export `TAZ_LITELLM_KEY` in the foreman's shell. It is passed to the coder only as
  `ANTHROPIC_AUTH_TOKEN`.

## For each course in `queue.txt`, in order

Each line is `<course file> <suggested template>`. Keep `$B/state.tsv` (file, result,
UTC time) so a restart resumes after the last course sent. Below, `F` is the course file,
`T` the suggested template and `S` the file name without `.yaml`.

1. **Fresh clone at the latest main, then a branch.**
   ```bash
   R=$B/work/$S/repo
   test -e $B/work/$S && mv $B/work/$S $B/work/$S.old-$(date -u +%Y%m%dT%H%M%S)
   git clone --quiet https://github.com/mblanke/TrueNorth.git $R
   git -C $R switch -c taz/course-$S
   ln -s $B/venv $R/.venv && echo .venv >> $R/.git/info/exclude
   ```
2. **Card.** Allowed paths are exactly the course file and its instructor pack. The card
   lives outside the clone.
   ```bash
   python3 $R/tools/taz/courses/make_card.py $F --repo-root $R > $B/cards/$S.json
   ```
   `make_card.py` refuses the seven held courses; if it refuses, record `held` and skip.
3. **Run the coder** (attempt `N` = 1, 2, 3), from inside the clone:
   ```bash
   cd $R && env -u ANTHROPIC_API_KEY -u CLAUDE_CODE_OAUTH_TOKEN -u GH_TOKEN -u GITHUB_TOKEN \
     -u SSH_AUTH_SOCK \
     ANTHROPIC_BASE_URL=http://127.0.0.1:4000 ANTHROPIC_AUTH_TOKEN="$TAZ_LITELLM_KEY" \
     ANTHROPIC_MODEL=taz-coder CLAUDE_CONFIG_DIR=$B/claude-config \
     TAZ_CARD=$B/cards/$S.json TAZ_GATE_POLICY=/opt/taz/hooks/policy.json \
     TAZ_STOP_STATE=$B/work/$S/stop-state \
     timeout 90m claude -p "$PROMPT" --model taz-coder --max-turns 150 \
       --permission-mode acceptEdits --output-format stream-json --verbose \
     > $B/logs/$S-attempt$N.jsonl 2>&1
   ```
   `$PROMPT` is: `Course file: $F. Suggested range template: $T.` followed by the full text
   of `tools/taz/courses/AGENTS-courses.md` from the clone. On attempts 2 and 3, append
   `The checker still fails. Its output:` and the checker output from step 4.
4. **Check it yourself.** The coder's report is not evidence.
   ```bash
   cd $R && .venv/bin/python scripts/check_course.py --base origin/main $F > $B/logs/$S-check$N.txt 2>&1; echo "EXIT=$?"
   git -C $R status --porcelain
   git -C $R diff --stat
   ```
   It passes only if the checker exit is 0 **and** `git status` lists nothing but `$F` and
   `content/courses/instructor/$S.yaml`. Anything else is a failed attempt; if a path
   outside the card changed, stop the course and report it blocked (the gate should have
   refused that write).
5. **On a pass**, commit and send the patch:
   ```bash
   git -C $R add $F content/courses/instructor/$S.yaml
   git -C $R -c user.name="TAZ course coder" -c user.email=taz-coder@truenorth.invalid \
     commit --quiet -m "content(courses): bring $S to the technically-proven bar (TAZ draft)"
   git -C $R format-patch -1 --stdout > $B/logs/$S.patch
   ```
   Send one Remote Control message to the Mac Claude session. First line exactly
   `TAZ course patch: <F>`, then the patch text, unedited.
6. **After 3 failed attempts**, send one message. First line exactly
   `TAZ course blocked: <F>`, then the last checker output and `git diff --stat`.
7. Record the result in `state.tsv` and go straight to the next course.

When the queue is done, send `TAZ course run finished:` with the `state.tsv` contents.

## What Claude does with each message (for reference)

Claude applies the patch with `git am` on a branch from current main, re-runs
`scripts/check_course.py --base origin/main`, reviews the labs against the template and the
quiz keys against the content, sets `readiness: technically_proven`, runs the gate and opens
the PR. The course still reaches Students only after SME/Standards sign-off and a course
release.
