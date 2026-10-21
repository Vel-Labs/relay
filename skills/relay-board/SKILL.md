---
name: relay-board
description: Use a separately owned local Relay project board for agent reports, questions and explicit message receipts.
---

# Relay board

Resolve the Relay root from this file's `../..` directory. Read its README.
Use that root's `app.py`; Python 3.10+ and the standard library suffice.
No account, provider, workspace adapter or installed daemon is required.

## Select the exact board

Obtain the assigned database, instance and session. Do not infer ownership from
a similar project label. Run `python3 -B app.py --db DATABASE instances`.
For a new project, fill `project.example.json` with the project ID, title,
objective and optional sessions. Register only when that board write is within
the task: `python3 -B app.py --db DATABASE create --json CONFIG`.
A scaffold-generated Relay configuration uses the same shape. Preserve its
instance ID across worktrees; genuinely new projects need new IDs.

A duplicate instance fails without replacing the original. Inspect the exact
ID after an uncertain result rather than invent a replacement.

## Read and report

Use `snapshot --instance EXACT_ID` and
`inbox --instance EXACT_ID --session EXACT_SESSION`. Reads never acknowledge
messages. `ingest` accepts attributed progress, questions, blockers, notes,
deliverables and summaries as JSON. Include source/evidence and a stable event
ID. Identical retries deduplicate; a changed payload with that ID is rejected.
Reports do not prove runtime liveness, correctness, approval or acceptance.
Summaries may name the actual model; Relay does not select or call one.

## Receive a message

After the assigned session actually receives the exact message, use CLI
`receipt` with instance, session, message ID, status and concrete note.
Progress through delivered, acknowledged and completed; use failed for failure.
A retry from failed must be verified before another delivered receipt.
A completed receipt concerns the message, not acceptance of product work.
The browser cannot write an agent's delivery receipts.

## Open and stop

When authorized, `python3 -B app.py --db DATABASE serve --port 0` prints the
exact loopback URL. Open that URL and select the assigned board. Stop only
your own server with Ctrl-C. Existing servers/databases keep their owners.
No agent transport, model dispatch, hook, schedule, webhook or publication
control is installed here. An agent on another computer needs an explicit
handoff. Do not imply a saved attachment is a live connection.
