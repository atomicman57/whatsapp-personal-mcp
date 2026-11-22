# Intake self-chat alerts

This change stacks on PR #5 (`fix/bare-number-labels`). No production deployment
is included. The flag is off unless `INTAKE_SELF_ALERT_ENABLED=true` is set.

Each detector writes a small alert job inside the transaction that creates a
new candidate/revision. An independent worker polls every 50 ms and contacts
**main**, even for secondary candidates. It never waits for the webhook and never
opens a chat or marks a message read. Queue creation performs no network I/O.
The normal aim is about one second after detection, subject to WhatsApp/network
latency. This is not a measured production guarantee.

The main bridge owns the alert ledger in its existing private messages database.
It resolves its connected own JID/LID and also requires the own phone to end in
1755. A submitted destination must exactly equal that own JID or LID. Numbers,
other contacts, group JIDs and arbitrary aliases are refused. The dedicated alert
credential grants only identity and alert routes, not general sending, reading,
contact changes or payments. The separate `WHATSAPP_SEND_ENABLED` setting does not
control this feature; its explicit `INTAKE_SELF_ALERT_ENABLED` flag does.

The alert includes the saved/contact display name or `unknown sender`, an explicit
amount without invented currency, bank, account last four and
`Approve in Transfer Request Bot`. A conservative explicit introduction such as
`this is Tunde,`, `my name is Ada.` or a standalone `from Ada` adds a `claimed_name`
field to the card. Names must start with a capital letter and end at punctuation
or the end of the message. Conflicting names produce no claim. Saved contact and
WhatsApp push names remain separate card fields. A claim is displayed only as
`claims to be X`. Digits and control characters are removed from name/bank labels.
Account numbers remain strings. No raw account or source-message text is in an
alert job, ledger or new log statement. Pending jobs contain only the formatted
alert, including its last-four mask, and clear that text on claim or abandonment.

## Revisions, deletion and crash behavior

The ledger contains `event_id`, `request_key`, `revision`, `message_id`, `chat_jid`,
`sent_at`, `deleted_at`, `delete_mode`, `status`, with uniqueness on event/revision.
Every candidate revision is mapped, including suppressed account-name-only or
purpose-only updates. New candidates send once; amount/bank/account-number changes
first delete all previous live alerts for that request, then send the replacement.
An older delayed revision never replaces a newer one. A failed prior deletion
blocks the replacement and remains visible for explicit cleanup.

Deletion resolves the event to its request and deletes all live rows, attempting
every live row even if another delete fails. It first sends whatsmeow
`BuildRevoke` with the original outgoing message ID, then uses the WhatsApp
`deleteMessageForMe` app-state mutation if revoke fails. `delete_mode=everyone`
means WhatsApp accepted the revoke request; it is not proof that every linked
client has synchronized. `me` means only delete-for-me succeeded. Failed rows stay
live as `delete_failed` and the tool returns partial results for explicit retry.
The list tool also exposes these rows.

Deletion records a revision cutoff and event tombstone, including when called
before the send arrives. It prevents late delivery of the deleted revision.
A later changed revision can produce a fresh owner alert. A withdrawal deletes
through the preceding event's request mapping.

A durable claim is committed before the WhatsApp call, together with a generated
WhatsApp message ID. If sending times out or the process crashes, no automatic
send retry occurs. The preallocated ID permits explicit revocation of an uncertain
send. `sent_at` for `sending`/`send_unknown` is the attempt time; `sent` records
confirmed completion. `claimed` means no WhatsApp send has started.

Startup never scans webhook outbox rows into alert jobs. Existing pending alert
jobs are abandoned and their payload cleared, rather than replayed after downtime.
Claims are never reclaimed. This chooses at-most-once sending over guaranteed
notification during a crash. Inspect `intake_alert_jobs.status` in the private
detector database and ledger status through the list tool when reconciling a crash.
No payment is executed, no WhatsApp reply constitutes approval, and no transfer
retry is introduced. Dry-run/test events require `INTAKE_SELF_ALERT_TEST_ENABLED=true`
on both the originating detector and main bridge in addition to the main flag.

## TRB tools

The existing owner OAuth subject and `whatsapp` scope are required. Configure the
main endpoint/token in each MCP server that exposes these tools.

```json
{"name":"delete_intake_alert","arguments":{"event_id":"<candidate-event-id>","reason":"approved"}}
{"name":"delete_intake_alert","arguments":{"event_id":"<candidate-event-id>","reason":"rejected"}}
{"name":"delete_intake_alert","arguments":{"event_id":"<candidate-event-id>","reason":"edited"}}
{"name":"list_intake_alerts","arguments":{"event_id":"<candidate-event-id>"}}
```

`reason` is optional, defaults to `approved`, and accepts a short lowercase label
with underscores, up to 32 characters. It never authorizes payment. Call deletion
after TRB's actual approved/rejected/edited state transition. Inspect `success` and
each row's status/delete mode. A failed deletion may be explicitly retried.

## Future deployment, separately authorized

1. Record the approved commit, existing image digests and configuration. Take
   consistent online backups of bridge and detector SQLite databases. Preserve
   live sessions, cursors, outbox/dedupe state and the webhook token.
2. Build the immutable image from the approved commit with the existing Dockerfile.
   Deploying it requires a separate maintenance approval. Never run two bridges
   with the same WhatsApp session. No temporary client may share the production
   session for a test.
3. Main bridge: configure `INTAKE_SELF_ALERT_ENABLED=true` and a separately
   provisioned private `INTAKE_SELF_ALERT_TOKEN` of at least 32 characters. Do not
   reuse or rotate the webhook token. Keep the test flag absent in normal use.
   Secondary bridge keeps its alert feature off.
4. Main detector: set the flag, scoped token and
   `INTAKE_SELF_ALERT_MAIN_URL=http://127.0.0.1:8753/api`. Both MCP processes need
   the same token and a reachable main URL for TRB deletion/listing. Main MCP can
   use that same loopback URL.
5. Secondary detector/MCP need a route to **main**, not secondary loopback. Provide
   a private authenticated TLS reverse proxy to main's bridge with only
   `/api/identity`, `/api/intake-alerts` and `/api/intake-alerts/delete` routed, then
   set `INTAKE_SELF_ALERT_MAIN_URL=https://<private-main-alert-origin>/api`.
   Alternatively use an operator-managed loopback tunnel in secondary's network
   namespace, targeting main's bridge, and configure its loopback port. Current
   container networking publishes only MCP, so this routing is a deployment
   dependency. Plain remote HTTP and embedded URL credentials are rejected.
6. Verify main identity ending 1755 and owner OAuth; run both suites on the exact
   image, check tools/list, both account detector health and live cursors. Enable
   the detector flags only for fresh intake. Never rewind/replay history. With
   separately authorized synthetic traffic verify last-four-only formatting,
   secondary-to-main routing, revision replacement and TRB deletion.
7. The authorized live primitive check for this job was blocked: deployed main
   has `/api/identity` but no revoke/delete/intake-alert routes. Authenticated
   GET checks returned 404 for those routes. No test message was sent and no
   latency or self-chat revocation result was claimed. A separately paired,
   isolated main-account session would be needed to test new code without
   replacing or sharing production's session, or a later authorized deployment.

Rollback: first turn off both detector alert flags, then main bridge's flag.
Keep main's upgraded bridge available for cleanup of already-sent alert IDs.
Explicitly delete remaining live alerts before reverting its image. Preserve all
live SQLite state, sessions and detector cursors when restoring recorded images.
Added tables can remain. Never restore an older messages database or replay old
outbox rows. If a rollback interrupts a send, use its saved ID for explicit
reconciliation and do not resend it.
