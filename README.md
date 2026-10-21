# Relay

A local place to see what your coding agents report, ask questions, and keep the
reply with the work. Each project has its own board; sessions sit underneath it.
A message can be saved without pretending it has been delivered or acted on.

Relay runs on Python 3.10+ and the standard library. It needs no account, API key,
model subscription, Node package, or particular workspace layout. [Agent setup instructions](skills/relay-board/SKILL.md) are included. Any agent with
local command access can read its inbox and report back through the same CLI.
An agent on another computer needs an explicit relay; this is not remote transport.

## Start your first board

Clone this repository, then from its root:

```sh
python3 -B app.py create --json project.example.json
python3 -B app.py serve --port 0
```

Open the exact `http://127.0.0.1:PORT` printed in the terminal. Choose your board,
attach a session, and use **Send a message**. Stop the server with Ctrl-C.
The default database is `.data/board.sqlite3` in this checkout. To choose another,
put `--db /absolute/owned/board.sqlite3` before each command. Preserve this file
when updating source. Stop the owned server before copying its database for backup.

## Connect an agent

Copy the connection handoff from the exact session card. Registering a session
does not launch an agent or connect an application. Give the receiving agent
the CLI location, database, instance and session ID:

```sh
python3 -B app.py inbox --instance my-project --session my-agent
python3 -B app.py snapshot --instance my-project
```

Reads never acknowledge messages. Reports and receipts accept JSON from stdin
or `--json FILE`. Publish a report:

```sh
python3 -B app.py ingest <<'JSON'
{"instance":"my-project","session":"my-agent","id":"finding-001","kind":"progress","status":"review","text":"The change is ready for review.","source":"My agent report","evidence":"tests/results.txt"}
JSON
```

After actually receiving a specific message, send a receipt:

```sh
python3 -B app.py receipt <<'JSON'
{"instance":"my-project","session":"my-agent","message":"EXACT_MESSAGE_ID","status":"delivered","note":"Received in the assigned session."}
JSON
```

Continue with `acknowledged`, then `completed`, adding concrete notes. Use
`failed` when delivery/action fails; a verified retry can start from delivered.
Receipt completion concerns the message, not acceptance of the project.
The browser cannot generate delivery receipts for an agent.

## What it provides

- Named boards, objectives, shared context and stable instance boundaries.
- Attached sessions with readable labels and self-reported model information.
- Progress, questions, blockers, findings, deliverables and summaries.
- A persistent outbox, per-session inboxes and explicit delivery history.
- Identical event/message retries deduplicate; conflicting content keeps the
  original and fails. Invalid session creation rolls back atomically.
- Keyboard-accessible forms, source/evidence disclosures, and light/dark themes.

Saved records are reports. They do not prove an agent is still running, establish
that its claims are correct, or grant approval. Questions requiring approval keep
an explicit wait default. There is no task executor, provider client, hook,
scheduler, webhook delivery or publication control in this standalone distribution.

## Use it with Project Scaffold

Generate your own project with Scaffold's optional `--with-relay`. Its
`skills/relay-board/templates/project.json` contains a unique stable board ID.
From this Relay checkout, register that exact configuration:

```sh
python3 -B app.py create --json /absolute/project/skills/relay-board/templates/project.json
```

Keep the ID across worktrees. A genuinely new project needs a new ID. Shared
Relay does not become your source repository, task acceptance owner or website.
It stores communication; the project keeps its own source, decisions and tests.
[Project Scaffold](https://github.com/Vel-Labs/project-scaffold) is optional.

## Checks and contributions

```sh
python3 -B test_relay.py
node --check app.js
```

Node is optional for contributors checking JavaScript syntax; it is not required
to run Relay. Tests use disposable SQLite data and temporary loopback listeners.
They make no model calls. Verify changed forms in a real browser too.

`board.py` owns records and validation; `app.py` owns CLI/HTTP boundaries;
`index.html`, `app.js` and `style.css` own presentation. Keep SQLite parameterized,
instance/session identity explicit, and HTTP same-origin/loopback protections intact.
Source updates must not overwrite `.data`, user messages or other writers' changes.

## Availability and origin

This distribution extracts the message board from an existing private workspace
prototype. Research executors, private histories, credentials and live databases
are deliberately absent. No connection to that workspace is required.

## License

[MIT](LICENSE). Existing third-party licenses remain with their owners.
