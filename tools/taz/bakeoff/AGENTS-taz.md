# Rules for this task

You are working alone on one ticket in a disposable clone of TrueNorth. Your diff and
the tests decide the outcome, not your summary.

1. **Grep before you use a name.** Before you call, import or reference a function, class,
   route, CSS class or file, find its definition with grep and read it. Do not assume a name
   exists because it would be sensible.
2. **Read the whole file before you edit it.** Not a window around the line: the whole file.
3. **Edit, never Write, an existing file.** Use Write only to create a new file. Rewriting an
   existing file drops every line you did not re-emit.
4. **Stay inside the ticket.** Change only the paths the ticket allows. Do not touch tests
   unless the ticket asks you to add or fix them, and never weaken, skip or delete an
   existing assertion to make something pass.
5. **Run the relevant tests** (the commands are below) and read their output. Fix what fails.
6. **Be honest at the end.** Finish with a short report: what you changed, which commands you
   ran, and their results. If you are blocked, ran out of budget, or could not run or pass
   the tests, start the report with `BLOCKED:` and say exactly what is missing. Never say the
   work is done, fixed or passing unless you ran the tests and they passed.
