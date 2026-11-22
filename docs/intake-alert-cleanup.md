# Intake alert tombstone cleanup

`delete_intake_alert` first revokes the owner's ledger alert, then submits
`deleteMessageForMe` on `regular_high` for the original outgoing message key and
any distinct revoke stanza ID returned by WhatsApp. A revoke failure still falls
back to delete-for-me for the original key. No chat history scan is involved.
Both revoke and app-state calls check that the chat matches the connected main
account's own phone JID or LID and the ID belongs to the intake alert ledger.
The existing main-account suffix check remains in place.

The additive SQLite migration preserves existing rows. New metadata is
`revoke_message_id`, `revoked_at`, `cleanup_at`, and `delete_error`. An acknowledged
revoke is saved before app-state calls. If cleanup fails, an explicit delete retry
uses those saved keys without issuing another revoke. `delete_mode` is
`revoke+delete_for_me` or `delete_for_me_only`; on a failed operation it describes
the attempted path. Check `status`, `deleted_at`, `cleanup_at` and tool `success`
for completion. Error codes identify `revoke_failed`,
`delete_for_me_original_failed`, and `delete_for_me_revoke_failed`, without storing
transport exception text. A successful fallback retains `revoke_failed` for audit.
A completed cleanup skips repeated calls, including after process restart.

## One-off cleanup

The owner OAuth subject and `whatsapp` scope are required for the MCP tool.
The bridge route `POST /api/intake-alerts/cleanup` uses the existing scoped intake
alert token or full bridge credential. Read-only credentials cannot mutate it.
An existing restricted reverse proxy must allow this new path before MCP can use
it remotely. No new credentials or configuration flags are introduced.

```json
{"name":"cleanup_intake_alert_tombstones","arguments":{"since":"2026-11-22T00:00:00+01:00"}}
{"name":"cleanup_intake_alert_tombstones","arguments":{"since":"2026-11-22T00:00:00+01:00","dry_run":false}}
```

`dry_run` defaults to true. The response has `data.count`, `data.cleaned`,
`data.dry_run`, and `data.rows`. Preview makes no app-state calls or ledger writes.
`since` is inclusive against the alert's original `sent_at`, with an explicit
timezone. Omitting it considers all eligible ledger alerts. The selection includes
only rows already deleted/revoked, including saved successful revokes whose
app-state cleanup failed. Live alerts and IDs supplied by callers are excluded.
A failed row retains its error and remains eligible for an explicit retry. Each
request has a ten-second deadline; successful rows remain skipped on later calls.

## Pinned whatsmeow support and limits

The dependency is `v0.0.0-20260718134955-fac667d55293`. It has no
`appstate.BuildDeleteForMe` helper. This change retains the existing supported
mutation format: index `deleteMessageForMe,<chat>,<message-id>,1,0`, version 3,
`DeleteMedia=false`, and the message timestamp in Unix seconds. `1` means
`FromMe`, which is appropriate for these outgoing self-alerts. The revoke uses
`BuildRevoke(chat, types.EmptyJID, originalID)`.

Source references at the pinned commit:

- [Revoke builder and outgoing send response ID](https://github.com/tulir/whatsmeow/blob/fac667d55293/send.go).
- [Delete-for-me index](https://github.com/tulir/whatsmeow/blob/fac667d55293/appstate/keys.go).
- [Delete-for-me event decoding and SendAppState](https://github.com/tulir/whatsmeow/blob/fac667d55293/appstate.go).

`SendAppState` synchronizes encrypted account app state through WhatsApp. It
requires existing app-state sync keys and can fail if they are unavailable. Its
success is server acknowledgment, not confirmation that any particular phone or
linked device has removed a visible tombstone. The pinned client does not forbid
this mutation for an own sent self-chat message, but this job has not tested
phone behavior, propagation timing, offline-device catch-up, or ordering between
revocation delivery and app-state synchronization. No WhatsApp messages were sent
for this change. Live UI verification is required before claiming the fix works
on the owner's phone or all linked devices.

Usually the displayed tombstone replaces the original key. The returned revoke
stanza ID is also cleaned when distinct, but is not proof of a separate visible
UI row. Legacy deleted rows did not save revoke IDs. Cleanup can target their
original ledger IDs only; it will never guess IDs or scan unrelated messages.
A crash between revoke acknowledgment and its ledger write can likewise lose
that returned ID. A transport timeout may have applied a mutation despite an
error; retries reapply the same deletion keys. `cleanup_at` marks acknowledgment,
so a completed row is skipped even if a device has not synchronized yet.

## Later deployment and live verification

This PR does not deploy, restart services, change remote configuration or send
WhatsApp traffic. Deployment and the following live test require separate
operator authorization.

1. Record the approved commit and existing image digests. Back up the live SQLite
   databases consistently. Preserve session files, detector cursors, ledger,
   outbox state, tokens and current alert flags.
2. Build the normal image from that commit. Update the main bridge and MCP server
   using the existing deployment procedure. The bridge migration adds only alert
   metadata columns. Keep exactly one bridge using the live WhatsApp session.
   Allow `/api/intake-alerts/cleanup` through any existing scoped proxy route.
3. Verify the main bridge's identity is the owner's self-chat, ending `1755`,
   and that `tools/list` exposes `cleanup_intake_alert_tombstones` to the owner.
4. With explicit authorization for one synthetic alert, use the existing intake
   test-event path with `INTAKE_SELF_ALERT_TEST_ENABLED=true` only for the test.
   Record its event ID, message ID and commit. Confirm exactly one alert appears
   in `2348133501755@s.whatsapp.net`. Avoid replaying detector history.
5. Call `delete_intake_alert` for that event. Confirm success,
   `delete_mode=revoke+delete_for_me`, `status=deleted`, `cleanup_at` populated,
   and no error. On the phone and each linked device, verify the alert and
   "You deleted this message" row are gone. Reconnect an offline linked device
   and verify catch-up. Report per-device results; acknowledgments alone are
   insufficient. Stop and investigate if a tombstone remains.
6. Preview today's older alerts with the first cleanup example above. Review
   `data.count` and every returned ledger row. Then run the second example with
   the same `since`. Verify `data.cleaned`, errors and visible tombstone removal
   on the phone and linked devices. Preview again; acknowledged rows should no
   longer be eligible. Adjust the date to the actual WAT test day if later.
7. Restore the prior test flag. Roll back the image if necessary while preserving
   databases and session files. Added columns can remain; never restore old
   detector state or replay an outbox to retry alerts.
