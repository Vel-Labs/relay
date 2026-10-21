# Relay contributor guidance

Keep the standalone board independent of workspace paths, model providers and
executors. `board.py` owns SQLite records and validation; `app.py` owns CLI and
HTTP; the existing web files own presentation. Preserve instance/session scope,
retry/conflict checks, explicit receipts and human approval boundaries.

Run `python3 -B test_relay.py` and `node --check app.js` after changes. Verify
changed forms in a browser using a temporary database. Never use a live database
for tests or claim that saved reports establish execution or acceptance.

Publish only reviewed source files. Exclude `.data`, private history, transcripts,
credentials and unrelated work. No daemon, scheduler, provider or hook is installed.
