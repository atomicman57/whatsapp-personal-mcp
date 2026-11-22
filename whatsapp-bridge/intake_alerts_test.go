package main

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/appstate"
	waProto "go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
)

const alertSelf = "15550001755@s.whatsapp.net"
const alertLID = "999001755@lid"

func alertFixture(t *testing.T) (*intakeAlerts, *[]string) {
	t.Helper()
	t.Setenv("INTAKE_SELF_ALERT_ENABLED", "true")
	db, err := sql.Open("sqlite3", filepath.Join(t.TempDir(), "alerts.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	if err = migrateIntakeAlerts(db); err != nil {
		t.Fatal(err)
	}
	calls := []string{}
	own, _ := types.ParseJID(alertSelf)
	lid, _ := types.ParseJID(alertLID)
	a := &intakeAlerts{db: db, own: func() (types.JID, types.JID) { return own, lid }, newID: func() string { return fmt.Sprintf("message-%d", len(calls)) },
		send: func(_ context.Context, jid types.JID, msg *waProto.Message, id string) (string, error) {
			if jid.String() != alertSelf && jid.String() != alertLID {
				t.Error("non-self send")
			}
			if !strings.HasSuffix(msg.GetConversation(), "Approve in Transfer Request Bot") {
				t.Error("missing approval instruction")
			}
			calls = append(calls, "send")
			return id, nil
		}, remove: func(_ context.Context, _ types.JID, id string, _ time.Time) (string, error) {
			calls = append(calls, "delete:"+id)
			return "everyone", nil
		}}
	return a, &calls
}

func alertInput(event string, revision int) intakeAlertInput {
	return intakeAlertInput{EventID: event, RequestKey: "request-one", Revision: revision, ChatJID: alertSelf,
		Text: "Demo: NGN 12,500; Wema; account ending 2345. Approve in Transfer Request Bot"}
}

func TestIntakeSelfGuard(t *testing.T) {
	a, calls := alertFixture(t)
	for _, target := range []string{"15550000002@s.whatsapp.net", "999000000@lid", "15550001755:2@s.whatsapp.net", "999000000@g.us", ""} {
		in := alertInput("event-one", 1)
		in.ChatJID = target
		if _, err := a.create(context.Background(), in); err == nil {
			t.Error("non-self destination accepted")
		}
	}
	if len(*calls) != 0 {
		t.Fatal("guard dispatched")
	}
	in := alertInput("event-lid", 1)
	in.ChatJID = alertLID
	if _, err := a.create(context.Background(), in); err != nil {
		t.Fatal(err)
	}
	a.own = func() (types.JID, types.JID) {
		return types.NewJID("15550000002", types.DefaultUserServer), types.EmptyJID
	}
	if _, err := a.create(context.Background(), alertInput("event-wrong-account", 2)); err == nil {
		t.Fatal("secondary bridge accepted")
	}
}

func TestIntakeIdempotencyAndRevision(t *testing.T) {
	a, calls := alertFixture(t)
	ctx := context.Background()
	for range 2 {
		if _, err := a.create(ctx, alertInput("event-one", 1)); err != nil {
			t.Fatal(err)
		}
	}
	if len(*calls) != 1 {
		t.Fatal("duplicate dispatched")
	}
	// Reopen service over the same persistent database, simulating restart.
	if err := migrateIntakeAlerts(a.db); err != nil {
		t.Fatal(err)
	}
	if _, err := a.create(ctx, alertInput("event-one", 1)); err != nil {
		t.Fatal(err)
	}
	rows, err := a.create(ctx, alertInput("event-two", 2))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(*calls, ",") != "send,delete:message-0,send" {
		t.Fatal(*calls)
	}
	if len(rows) != 2 || rows[0].DeletedAt == "" || rows[0].DeleteMode != "everyone" || rows[1].Status != "sent" {
		t.Fatal(rows)
	}
	if _, err = a.create(ctx, alertInput("event-delayed", 1)); err != nil {
		t.Fatal(err)
	}
	if len(*calls) != 3 {
		t.Fatal("delayed revision sent")
	}
	rows, err = a.delete(ctx, "event-one")
	if err != nil {
		t.Fatal(err)
	}
	if rows[len(rows)-1].Status != "deleted" {
		t.Fatal(rows)
	}
	if _, err = a.delete(ctx, "event-two"); err != nil {
		t.Fatal(err)
	}
	if len(*calls) != 4 {
		t.Fatal("duplicate delete")
	}
	// A fresh changed revision is a new request for owner review.
	if _, err = a.create(ctx, alertInput("event-three", 3)); err != nil {
		t.Fatal(err)
	}
	if len(*calls) != 5 {
		t.Fatal("fresh revision suppressed")
	}
}

func TestIntakeConcurrentDuplicate(t *testing.T) {
	a, calls := alertFixture(t)
	var wg sync.WaitGroup
	for range 12 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, err := a.create(context.Background(), alertInput("event-one", 1)); err != nil {
				t.Error(err)
			}
		}()
	}
	wg.Wait()
	if len(*calls) != 1 {
		t.Fatal("duplicate send")
	}
}

func TestIntakeFlagsAndPrivacy(t *testing.T) {
	a, calls := alertFixture(t)
	ctx := context.Background()
	t.Setenv("INTAKE_SELF_ALERT_ENABLED", "")
	if _, err := a.create(ctx, alertInput("event-one", 1)); err == nil {
		t.Fatal("default enabled")
	}
	t.Setenv("INTAKE_SELF_ALERT_ENABLED", "true")
	in := alertInput("event-test", 1)
	in.Test = true
	if _, err := a.create(ctx, in); err == nil {
		t.Fatal("test sent without explicit flag")
	}
	in = alertInput("event-full-number", 1)
	in.Text = "Demo 0000012345 Approve in Transfer Request Bot"
	if _, err := a.create(ctx, in); err == nil {
		t.Fatal("full account accepted")
	}
	if len(*calls) != 0 {
		t.Fatal("unexpected send")
	}
	t.Setenv("INTAKE_SELF_ALERT_TEST_ENABLED", "true")
	in = alertInput("event-test", 1)
	in.Test = true
	if _, err := a.create(ctx, in); err != nil {
		t.Fatal(err)
	}
}

func TestIntakeSuppressedMappingAndDeleteBeforeArrival(t *testing.T) {
	a, calls := alertFixture(t)
	ctx := context.Background()
	if _, err := a.create(ctx, alertInput("event-one", 1)); err != nil {
		t.Fatal(err)
	}
	in := alertInput("event-name-only", 2)
	in.Suppress = true
	if _, err := a.create(ctx, in); err != nil {
		t.Fatal(err)
	}
	if len(*calls) != 1 {
		t.Fatal("name-only revision resent")
	}
	if _, err := a.delete(ctx, in.EventID); err != nil {
		t.Fatal(err)
	}
	if len(*calls) != 2 {
		t.Fatal("name-only event did not resolve previous alert")
	}
	if _, err := a.delete(ctx, "event-not-arrived"); err != nil {
		t.Fatal(err)
	}
	if _, err := a.create(ctx, alertInput("event-not-arrived", 3)); err != nil {
		t.Fatal(err)
	}
	if len(*calls) != 2 {
		t.Fatal("closed event sent late")
	}
}

func TestIntakeAmbiguousSendNeverRetriedAndRemainsDeletable(t *testing.T) {
	a, _ := alertFixture(t)
	count := 0
	a.send = func(_ context.Context, _ types.JID, _ *waProto.Message, _ string) (string, error) {
		count++
		return "", errors.New("synthetic network failure")
	}
	for range 2 {
		_, _ = a.create(context.Background(), alertInput("event-one", 1))
	}
	if count != 1 {
		t.Fatal("ambiguous send retried")
	}
	rows, err := a.rows("event-one")
	if err != nil {
		t.Fatal(err)
	}
	if rows[0].Status != "send_unknown" || rows[0].MessageID == "" {
		t.Fatal("lost ambiguous message identity")
	}
	rows, err = a.delete(context.Background(), "event-one")
	if err != nil {
		t.Fatal(err)
	}
	if rows[0].Status != "deleted" {
		t.Fatal(rows)
	}
}

func TestIntakeDeleteFailureBlocksReplacement(t *testing.T) {
	a, calls := alertFixture(t)
	ctx := context.Background()
	if _, err := a.create(ctx, alertInput("event-one", 1)); err != nil {
		t.Fatal(err)
	}
	a.remove = func(context.Context, types.JID, string, time.Time) (string, error) {
		return "", errors.New("synthetic failure")
	}
	if _, err := a.create(ctx, alertInput("event-two", 2)); err == nil {
		t.Fatal("replacement after deletion failure")
	}
	if len(*calls) != 1 {
		t.Fatal("replacement sent")
	}
	rows, err := a.rows("event-one")
	if err != nil {
		t.Fatal(err)
	}
	if rows[0].Status != "delete_failed" || rows[0].DeletedAt != "" {
		t.Fatal(rows)
	}
}

func TestIntakeRevokeAndFallback(t *testing.T) {
	own, _ := types.ParseJID(alertSelf)
	client := whatsmeow.NewClient(&store.Device{ID: &own}, nil)
	for _, fallback := range []bool{false, true} {
		t.Run(fmt.Sprint(fallback), func(t *testing.T) {
			patches := 0
			send := func(_ context.Context, jid types.JID, msg *waProto.Message) (string, error) {
				if msg.GetProtocolMessage().GetKey().GetID() != "original" || !msg.GetProtocolMessage().GetKey().GetFromMe() {
					t.Fatal("invalid revoke")
				}
				if fallback {
					return "", errors.New("synthetic revoke failure")
				}
				return "revoke", nil
			}
			apply := func(_ context.Context, p appstate.PatchInfo) error {
				patches++
				m := p.Mutations[0]
				if strings.Join(m.Index, ",") != "deleteMessageForMe,"+alertSelf+",original,1,0" || m.Value.GetDeleteMessageForMeAction().GetMessageTimestamp() != 1000 {
					t.Fatal("invalid delete-for-me patch")
				}
				return nil
			}
			mode, err := deleteIntakeMessage(context.Background(), client, send, own, "original", time.Unix(1000, 0), apply)
			if err != nil {
				t.Fatal(err)
			}
			if fallback && (mode != "me" || patches != 1) {
				t.Fatal(mode, patches)
			}
			if !fallback && (mode != "everyone" || patches != 0) {
				t.Fatal(mode, patches)
			}
		})
	}
}

func TestIntakeScopedAuthAndDeleteRoute(t *testing.T) {
	a, calls := alertFixture(t)
	ctx := context.Background()
	if _, err := a.create(ctx, alertInput("event-one", 1)); err != nil {
		t.Fatal(err)
	}
	token := strings.Repeat("a", 32)
	t.Setenv("INTAKE_SELF_ALERT_TOKEN", token)
	t.Setenv("WHATSAPP_BRIDGE_TOKEN", strings.Repeat("b", 32))
	t.Setenv("WHATSAPP_BRIDGE_READ_TOKEN", strings.Repeat("c", 32))
	mux := http.NewServeMux()
	a.routes(mux)
	handler := protectBridgeAccess(mux)
	for _, tc := range []struct {
		path, auth string
		want       int
	}{
		{"/api/send", token, 401}, {"/api/intake-alerts/delete", strings.Repeat("c", 32), 403},
		{"/api/intake-alerts/delete", token, 200},
	} {
		r := httptest.NewRequest("POST", tc.path, strings.NewReader(`{"event_id":"event-one","reason":"approved"}`))
		r.Header.Set("Authorization", "Bearer "+tc.auth)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		if w.Code != tc.want {
			t.Fatalf("%s status %d want %d", tc.path, w.Code, tc.want)
		}
	}
	if len(*calls) != 2 {
		t.Fatal("delete route did not delete")
	}
}

func TestIntakePartialDeleteReturnsAllResults(t *testing.T) {
	a, calls := alertFixture(t)
	ctx := context.Background()
	if _, err := a.create(ctx, alertInput("event-one", 1)); err != nil {
		t.Fatal(err)
	}
	_, err := a.db.Exec(`INSERT INTO intake_alerts(event_id,request_key,revision,message_id,chat_jid,sent_at,status) VALUES('event-two','request-one',2,'second',?,?,'sent')`, alertSelf, time.Now().UTC().Format(time.RFC3339Nano))
	if err != nil {
		t.Fatal(err)
	}
	attempted := 0
	a.remove = func(_ context.Context, _ types.JID, id string, _ time.Time) (string, error) {
		attempted++
		if id == "message-0" {
			return "", errors.New("synthetic failure")
		}
		return "me", nil
	}
	rows, err := a.delete(ctx, "event-two")
	if err == nil || attempted != 2 || len(rows) != 2 {
		t.Fatal("partial delete lost results", err, attempted)
	}
	if rows[0].Status != "delete_failed" || rows[1].DeleteMode != "me" || rows[1].Status != "deleted" {
		t.Fatal(rows)
	}
	if len(*calls) != 1 {
		t.Fatal("unexpected send")
	}
}

func TestIntakeFallbackFailureReported(t *testing.T) {
	own, _ := types.ParseJID(alertSelf)
	client := whatsmeow.NewClient(&store.Device{ID: &own}, nil)
	send := func(context.Context, types.JID, *waProto.Message) (string, error) {
		return "", errors.New("synthetic failure")
	}
	apply := func(context.Context, appstate.PatchInfo) error { return errors.New("synthetic patch failure") }
	mode, err := deleteIntakeMessage(context.Background(), client, send, own, "original", time.Now(), apply)
	if err == nil || mode != "" {
		t.Fatal("failed fallback reported success")
	}
}
