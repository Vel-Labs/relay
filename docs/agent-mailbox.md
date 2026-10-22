# Offline attachment mailbox

This optional CLI validates and retains data-only handoff envelopes. It has no
webhook, provider connection, background polling, bot dispatch or task executor.
It does not use Relay's board database. The standalone board remains independent.

## Files and compatibility

`python3 -I -B scripts/agent_mailbox.py` uses only the Python standard library
and the JSON contracts under `schemas/agent-mailbox/`. Python 3.10+ is required.
The existing tests exercise the supported schema subset; this is not a general
JSON Schema implementation. Keep the contracts and code at the same revision.

The retained example route/project identity is bound to the original consumer.
Changing a role name, project identity or schema is a compatibility change,
not a way to grant another agent permission. The four examples contain synthetic
UUIDs and simulated receipts; they are not real messages or proof of delivery.

## Inspect before retaining

```sh
python3 -I -B scripts/agent_mailbox.py validate schemas/agent-mailbox/examples/social-packet.json
python3 -I -B scripts/agent_mailbox.py --store /absolute/private/fixture-mailbox list
```

Listing a missing store does not create it. For mutation syntax, use `--help`
and a deliberately selected disposable/private store. Never point a rehearsal
at an existing board, live mailbox, profile or data store. Writing commands have
`--dry-run`; it must not create directories, locks, files or receipts.

The default store is `data/mailbox/`. A caller must authorize any real retention
and external attachment transfer separately. Preparing or exporting a file does
not send it. No installation, schedule, transport or account setup is included.

## Identity, duplicates and receipts

The CLI checks bounded JSON, duplicate keys, envelope identity, supported payload
fields, dates, sensitivity declarations and credential-like content before writes.
Sensitive or malformed input fails closed. This screening is not general proof
that arbitrary text is safe to disclose.

Immutable IDs retain the first bytes. Identical retries are duplicates; changed
bytes under the same ID are conflicts. Exclusive atomic publication and locks
prevent clobbering. Replies must join the original message, thread and content
hash. Validation never executes the requested work.

`ack` means reported storage, not handling. `done` requires reported handling
checks, but still does not establish correctness, owner acceptance or publication.
Conflicting reports remain visible. Missing receipts stay missing.

## Offline checks

```sh
python3 -B scripts/test_agent_mailbox.py
python3 -B test_relay.py
node --check app.js
```

Use temporary stores and no network for the mailbox tests. The standalone board
tests may use a temporary loopback HTTP fixture; they do not start a persistent
service. Synthetic credential strings in negative tests must remain rejected
and must not appear in error output. Preserve duplicate/conflict, late-reply,
no-clobber, interrupted-write and dry-run assertions.

No actual trial records, transcripts, discussions, credentials or internal
Dashboard history belong in this public source addition.
