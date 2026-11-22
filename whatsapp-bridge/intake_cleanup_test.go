package main

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/appstate"
	waProto "go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
)

func TestIntakeCleanupGuard(t *testing.T) {
	a, _ := alertFixture(t)
	if _, err := a.create(context.Background(), alertInput("one", 1)); err != nil {
		t.Fatal(err)
	}
	dispatched := 0
	a.apply = func(context.Context, appstate.PatchInfo) error { dispatched++; return nil }
	for _, tc := range []struct{ chat, id string }{{alertSelf, "untracked"}, {"15550000002@s.whatsapp.net", "message-0"}, {alertSelf, ""}} {
		jid, _ := types.ParseJID(tc.chat)
		if err := a.applyGuarded(context.Background(), deleteForMePatch(jid, tc.id, time.Now())); err == nil {
			t.Fatal("unsafe patch accepted", tc)
		}
		if err := a.deleteRows(context.Background(), []intakeAlertRow{{EventID: "one", Revision: 1, ChatJID: tc.chat, MessageID: tc.id, SentAt: time.Now().Format(time.RFC3339Nano)}}); tc.id != "" && err == nil {
			t.Fatal("unsafe revoke accepted", tc)
		}
	}
	if dispatched != 0 {
		t.Fatal("guard sent patch")
	}
}

func TestIntakeCleanupDryRunSinceIdempotency(t *testing.T) {
	a, calls := alertFixture(t)
	ctx := context.Background()
	if _, err := a.create(ctx, alertInput("one", 1)); err != nil {
		t.Fatal(err)
	}
	if _, err := a.db.Exec(`UPDATE intake_alerts SET status='deleted',deleted_at='2026-11-22T12:00:00Z',delete_mode='everyone',sent_at='2026-11-22T10:00:00Z'`); err != nil {
		t.Fatal(err)
	}
	// A live ledger alert and an unrelated message are never cleanup candidates.
	if _, err := a.db.Exec(`INSERT INTO intake_alerts(event_id,request_key,revision,chat_jid,message_id,sent_at,status) VALUES('live','live',1,?,'live-id','2026-11-22T10:00:00Z','sent')`, alertSelf); err != nil {
		t.Fatal(err)
	}
	patched := []string{}
	a.apply = func(_ context.Context, p appstate.PatchInfo) error {
		patched = append(patched, p.Mutations[0].Index[2])
		return nil
	}
	result, err := a.cleanup(ctx, time.Time{}, true)
	if err != nil || result.Count != 1 || result.Cleaned != 0 || len(patched) != 0 {
		t.Fatal(result, err, patched)
	}
	rows, _ := a.rows("one")
	if rows[0].CleanupAt != "" {
		t.Fatal("dry run wrote ledger")
	}
	since, _ := time.Parse(time.RFC3339, "2026-11-22T10:00:01Z")
	result, err = a.cleanup(ctx, since, true)
	if err != nil || result.Count != 0 {
		t.Fatal(result, err)
	}
	since, _ = time.Parse(time.RFC3339, "2026-11-22T10:00:00Z")
	result, err = a.cleanup(ctx, since, false)
	if err != nil || result.Cleaned != 1 || strings.Join(patched, ",") != "message-0" || result.Rows[0].DeleteMode != "revoke+delete_for_me" {
		t.Fatal(result, err, patched)
	}
	for range 2 {
		if err = migrateIntakeAlerts(a.db); err != nil {
			t.Fatal(err)
		}
		result, err = a.cleanup(ctx, time.Time{}, false)
		if err != nil || result.Count != 0 || len(patched) != 1 || len(*calls) != 1 {
			t.Fatal("cleanup repeated or revoked", result, err, patched, *calls)
		}
	}
}

func TestIntakeRevokeCleanupRetryAndErrors(t *testing.T) {
	for _, fallback := range []bool{false, true} {
		t.Run(map[bool]string{false: "revoke", true: "fallback"}[fallback], func(t *testing.T) {
			a, _ := alertFixture(t)
			ctx := context.Background()
			if _, err := a.create(ctx, alertInput("one", 1)); err != nil {
				t.Fatal(err)
			}
			own, _ := types.ParseJID(alertSelf)
			client := whatsmeow.NewClient(&store.Device{ID: &own}, nil)
			revokes := 0
			send := func(_ context.Context, _ types.JID, _ *waProto.Message) (string, error) {
				revokes++
				if fallback {
					return "", errors.New("secret transport payload")
				}
				return "revoke-key", nil
			}
			fail := true
			patched := []string{}
			a.apply = func(_ context.Context, p appstate.PatchInfo) error {
				id := p.Mutations[0].Index[2]
				patched = append(patched, id)
				// Revoke metadata must already be durable when the first patch is sent.
				rows, err := a.rows("one")
				if err != nil {
					t.Fatal(err)
				}
				if !fallback && rows[0].RevokeID != "revoke-key" {
					t.Fatal("revoke not durable", rows)
				}
				if fail {
					return errors.New("secret patch payload")
				}
				return nil
			}
			a.remove = func(ctx context.Context, jid types.JID, id string, sent time.Time) (string, error) {
				return a.removeWithClient(ctx, client, send, jid, id, sent)
			}
			rows, err := a.delete(ctx, "one")
			if err == nil || rows[0].Status != "delete_failed" || rows[0].DeletedAt != "" || strings.Contains(rows[0].DeleteError, "secret") {
				t.Fatal(rows, err)
			}
			if fallback && !strings.Contains(rows[0].DeleteError, "revoke_failed") {
				t.Fatal(rows)
			}
			fail = false
			rows, err = a.delete(ctx, "one")
			if err != nil || rows[0].Status != "deleted" || rows[0].CleanupAt == "" {
				t.Fatal(rows, err)
			}
			wantRevokes := 1
			wantMode := "revoke+delete_for_me"
			if fallback {
				wantRevokes = 2
				wantMode = "delete_for_me_only"
				if rows[0].DeleteError != "revoke_failed" {
					t.Fatal(rows)
				}
			}
			if revokes != wantRevokes || rows[0].DeleteMode != wantMode {
				t.Fatal(rows, revokes)
			}
			before := len(patched)
			if _, err = a.delete(ctx, "one"); err != nil || len(patched) != before || revokes != wantRevokes {
				t.Fatal("delete repeated", err)
			}
			if !fallback && strings.Join(patched, ",") != "message-0,revoke-key,message-0,revoke-key" {
				t.Fatal(patched)
			}
		})
	}
}

func TestIntakeCleanupFailureRetryAndSelfGuard(t *testing.T) {
	a, _ := alertFixture(t)
	ctx := context.Background()
	if _, err := a.create(ctx, alertInput("one", 1)); err != nil {
		t.Fatal(err)
	}
	if _, err := a.db.Exec(`UPDATE intake_alerts SET status='deleted',deleted_at='2026-11-22T12:00:00Z',revoke_message_id='revoke-key',revoked_at='2026-11-22T12:00:00Z'`); err != nil {
		t.Fatal(err)
	}
	calls := 0
	a.apply = func(context.Context, appstate.PatchInfo) error { calls++; return errors.New("secret") }
	result, err := a.cleanup(ctx, time.Time{}, false)
	if err == nil || result.Cleaned != 0 || calls != 2 || result.Rows[0].CleanupAt != "" || strings.Contains(result.Rows[0].DeleteError, "secret") {
		t.Fatal(result, err, calls)
	}
	a.apply = func(context.Context, appstate.PatchInfo) error { calls++; return nil }
	result, err = a.cleanup(ctx, time.Time{}, false)
	if err != nil || result.Cleaned != 1 || calls != 4 {
		t.Fatal(result, err, calls)
	}
	if _, err = a.db.Exec(`UPDATE intake_alerts SET cleanup_at='',chat_jid='15550000002@s.whatsapp.net'`); err != nil {
		t.Fatal(err)
	}
	if _, err = a.cleanup(ctx, time.Time{}, false); err == nil || calls != 4 {
		t.Fatal("non-self cleanup accepted")
	}
}

func TestIntakeCleanupRouteDefaultsAndAuth(t *testing.T) {
	a, _ := alertFixture(t)
	mux := http.NewServeMux()
	a.routes(mux)
	token := strings.Repeat("a", 32)
	t.Setenv("INTAKE_SELF_ALERT_TOKEN", token)
	t.Setenv("WHATSAPP_BRIDGE_TOKEN", strings.Repeat("b", 32))
	t.Setenv("WHATSAPP_BRIDGE_READ_TOKEN", strings.Repeat("c", 32))
	handler := protectBridgeAccess(mux)
	for _, tc := range []struct {
		body, auth string
		status     int
		contains   string
	}{
		{`{}`, token, 200, `"dry_run":true`},
		{`{"dry_run":false}`, token, 200, `"dry_run":false`},
		{`{"since":"2026-11-22"}`, token, 400, "since must"},
		{`{"message_id":"arbitrary"}`, token, 400, "Invalid"},
		{`{}`, strings.Repeat("c", 32), 403, "Read-only"},
		{`{}`, "invalid", 401, "Unauthorized"},
	} {
		r := httptest.NewRequest("POST", "/api/intake-alerts/cleanup", strings.NewReader(tc.body))
		r.Header.Set("Authorization", "Bearer "+tc.auth)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		if w.Code != tc.status || !strings.Contains(w.Body.String(), tc.contains) {
			t.Fatal(w.Code, w.Body.String())
		}
	}
}

func TestIntakeCleanupMigrationPreservesLegacyRows(t *testing.T) {
	a, _ := alertFixture(t)
	if _, err := a.create(context.Background(), alertInput("legacy", 1)); err != nil {
		t.Fatal(err)
	}
	for _, column := range []string{"revoke_message_id", "revoked_at", "cleanup_at", "delete_error"} {
		if _, err := a.db.Exec("ALTER TABLE intake_alerts DROP COLUMN " + column); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := a.db.Exec(`UPDATE intake_alerts SET deleted_at='2026-11-22T12:00:00Z',status='deleted',delete_mode='everyone'`); err != nil {
		t.Fatal(err)
	}
	for range 2 {
		if err := migrateIntakeAlerts(a.db); err != nil {
			t.Fatal(err)
		}
	}
	result, err := a.cleanup(context.Background(), time.Time{}, true)
	if err != nil || result.Count != 1 || result.Rows[0].RevokeID != "" || result.Rows[0].DeleteMode != "everyone" {
		t.Fatal(result, err)
	}
}
