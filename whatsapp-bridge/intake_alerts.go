package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"regexp"
	"strings"
	"sync"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/appstate"
	waProto "go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waSyncAction"
	"go.mau.fi/whatsmeow/types"
	"google.golang.org/protobuf/proto"
)

// A single main-bridge service owns both accounts' alerts. Claims are durable
// before network I/O and never automatically retried after an ambiguous send.
type intakeAlerts struct {
	mu     sync.Mutex
	db     *sql.DB
	own    func() (types.JID, types.JID)
	send   func(context.Context, types.JID, *waProto.Message, string) (string, error)
	newID  func() string
	apply  func(context.Context, appstate.PatchInfo) error
	remove func(context.Context, types.JID, string, time.Time) (string, error)
}

type intakeAlertInput struct {
	EventID    string `json:"event_id"`
	RequestKey string `json:"request_key"`
	Revision   int    `json:"revision"`
	ChatJID    string `json:"chat_jid"`
	Text       string `json:"text"`
	Suppress   bool   `json:"suppress"`
	Test       bool   `json:"test"`
}

type intakeAlertRow struct {
	EventID     string `json:"event_id"`
	RequestKey  string `json:"request_key"`
	Revision    int    `json:"revision"`
	MessageID   string `json:"message_id"`
	ChatJID     string `json:"chat_jid"`
	SentAt      string `json:"sent_at"`
	DeletedAt   string `json:"deleted_at"`
	DeleteMode  string `json:"delete_mode"`
	Status      string `json:"status"`
	RevokeID    string `json:"revoke_message_id"`
	RevokedAt   string `json:"revoked_at"`
	CleanupAt   string `json:"cleanup_at"`
	DeleteError string `json:"delete_error"`
}

var intakeID = regexp.MustCompile(`^[a-zA-Z0-9_-]{1,128}$`)
var intakeLongDigits = regexp.MustCompile(`[0-9](?:[\s-]*[0-9]){4,}`)

func migrateIntakeAlerts(db *sql.DB) error {
	_, err := db.Exec(`CREATE TABLE IF NOT EXISTS intake_alerts (
 event_id TEXT NOT NULL, request_key TEXT NOT NULL, revision INTEGER NOT NULL,
 message_id TEXT NOT NULL DEFAULT '', chat_jid TEXT NOT NULL,
 sent_at TEXT NOT NULL DEFAULT '', deleted_at TEXT NOT NULL DEFAULT '',
 delete_mode TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
 PRIMARY KEY(event_id,revision));
 CREATE INDEX IF NOT EXISTS intake_alert_request ON intake_alerts(request_key);
 CREATE TABLE IF NOT EXISTS intake_alert_closed (request_key TEXT PRIMARY KEY, revision INTEGER NOT NULL);
 CREATE TABLE IF NOT EXISTS intake_alert_closed_events (event_id TEXT PRIMARY KEY);`)
	if err != nil {
		return err
	}
	columns := map[string]bool{}
	rows, err := db.Query("PRAGMA table_info(intake_alerts)")
	if err != nil {
		return err
	}
	for rows.Next() {
		var cid, notNull, pk int
		var name, kind string
		var defaultValue any
		if err = rows.Scan(&cid, &name, &kind, &notNull, &defaultValue, &pk); err != nil {
			_ = rows.Close()
			return err
		}
		columns[name] = true
	}
	err = rows.Err()
	_ = rows.Close()
	if err != nil {
		return err
	}
	for _, name := range []string{"revoke_message_id", "revoked_at", "cleanup_at", "delete_error"} {
		if !columns[name] {
			if _, err = db.Exec("ALTER TABLE intake_alerts ADD COLUMN " + name + " TEXT NOT NULL DEFAULT ''"); err != nil {
				return err
			}
		}
	}
	return nil
}

func (a *intakeAlerts) self(target string) (types.JID, error) {
	own, lid := a.own()
	own = own.ToNonAD()
	lid = lid.ToNonAD()
	// Fixed account suffix is an additional main-account check, never a recipient.
	if own.Server != types.DefaultUserServer || !strings.HasSuffix(own.User, "1755") {
		return types.EmptyJID, fmt.Errorf("main identity unavailable")
	}
	if target != own.String() && (lid.IsEmpty() || target != lid.String()) {
		return types.EmptyJID, fmt.Errorf("self destination required")
	}
	jid, err := types.ParseJID(target)
	return jid, err
}

func (a *intakeAlerts) rows(eventID string) ([]intakeAlertRow, error) {
	rows, err := a.db.Query(`SELECT event_id,request_key,revision,message_id,chat_jid,sent_at,deleted_at,delete_mode,status,revoke_message_id,revoked_at,cleanup_at,delete_error
 FROM intake_alerts WHERE request_key IN (SELECT request_key FROM intake_alerts WHERE event_id=?) ORDER BY revision`, eventID)
	if err != nil {
		return nil, err
	}
	defer func() { _ = rows.Close() }()
	result := []intakeAlertRow{}
	for rows.Next() {
		var r intakeAlertRow
		if err = rows.Scan(&r.EventID, &r.RequestKey, &r.Revision, &r.MessageID, &r.ChatJID, &r.SentAt, &r.DeletedAt, &r.DeleteMode, &r.Status, &r.RevokeID, &r.RevokedAt, &r.CleanupAt, &r.DeleteError); err != nil {
			return nil, err
		}
		result = append(result, r)
	}
	return result, rows.Err()
}

func (a *intakeAlerts) deleteRows(ctx context.Context, rows []intakeAlertRow) error {
	var deletionErr error
	for _, row := range rows {
		if row.MessageID == "" || row.DeletedAt != "" {
			continue
		}
		jid, err := a.ledgerTarget(row.ChatJID, row.MessageID)
		if err != nil {
			return err
		}
		sent, err := time.Parse(time.RFC3339Nano, row.SentAt)
		if err != nil {
			return err
		}
		mode, err := a.remove(ctx, jid, row.MessageID, sent)
		if err != nil {
			_, updateErr := a.db.Exec(`UPDATE intake_alerts SET status='delete_failed',delete_mode=?,delete_error=? WHERE event_id=? AND revision=?`, mode, err.Error(), row.EventID, row.Revision)
			if updateErr != nil {
				return updateErr
			}
			deletionErr = fmt.Errorf("alert deletion failed")
			continue
		}
		_, err = a.db.Exec(`UPDATE intake_alerts SET deleted_at=?,cleanup_at=?,delete_mode=?,status='deleted' WHERE event_id=? AND revision=?`, time.Now().UTC().Format(time.RFC3339Nano), time.Now().UTC().Format(time.RFC3339Nano), mode, row.EventID, row.Revision)
		if err != nil {
			return err
		}
	}
	return deletionErr
}

func (a *intakeAlerts) create(ctx context.Context, in intakeAlertInput) ([]intakeAlertRow, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	jid, err := a.self(in.ChatJID)
	if err != nil {
		return nil, err
	}
	if !intakeID.MatchString(in.EventID) || !intakeID.MatchString(in.RequestKey) || in.Revision < 1 || len(in.Text) > 600 ||
		!strings.HasSuffix(in.Text, "Approve in Transfer Request Bot") || intakeLongDigits.MatchString(in.Text) {
		return nil, fmt.Errorf("invalid alert")
	}
	if !strings.EqualFold(os.Getenv("INTAKE_SELF_ALERT_ENABLED"), "true") ||
		(in.Test && !strings.EqualFold(os.Getenv("INTAKE_SELF_ALERT_TEST_ENABLED"), "true")) {
		return nil, fmt.Errorf("alerts disabled")
	}
	// Sending identical event/revision twice, including after restart, is forbidden.
	var count int
	if err = a.db.QueryRow(`SELECT count(*) FROM intake_alerts WHERE event_id=? AND revision=?`, in.EventID, in.Revision).Scan(&count); err != nil {
		return nil, err
	}
	if count > 0 {
		return a.rows(in.EventID)
	}
	// Persist every revision mapping, including stale/suppressed revisions.
	_, err = a.db.Exec(`INSERT INTO intake_alerts(event_id,request_key,revision,chat_jid,status) VALUES(?,?,?,?,'claimed')`, in.EventID, in.RequestKey, in.Revision, in.ChatJID)
	if err != nil {
		return nil, err
	}
	if err = a.db.QueryRow(`SELECT count(*) FROM intake_alert_closed WHERE request_key=? AND revision>=?`, in.RequestKey, in.Revision).Scan(&count); err != nil {
		return nil, err
	}
	var newer int
	if err = a.db.QueryRow(`SELECT count(*) FROM intake_alerts WHERE request_key=? AND revision>?`, in.RequestKey, in.Revision).Scan(&newer); err != nil {
		return nil, err
	}
	var closedEvent int
	if err = a.db.QueryRow(`SELECT count(*) FROM intake_alert_closed_events WHERE event_id=?`, in.EventID).Scan(&closedEvent); err != nil {
		return nil, err
	}
	if closedEvent > 0 {
		if _, err = a.db.Exec(`INSERT INTO intake_alert_closed VALUES(?,?) ON CONFLICT(request_key) DO UPDATE SET revision=max(revision,excluded.revision)`, in.RequestKey, in.Revision); err != nil {
			return nil, err
		}
	}
	if count > 0 || newer > 0 || closedEvent > 0 || in.Suppress {
		_, err = a.db.Exec(`UPDATE intake_alerts SET status='suppressed' WHERE event_id=? AND revision=?`, in.EventID, in.Revision)
		if err != nil {
			return nil, err
		}
		return a.rows(in.EventID)
	}
	prior, err := a.rows(in.EventID)
	if err != nil {
		return nil, err
	}
	if err = a.deleteRows(ctx, prior); err != nil {
		return nil, err
	}
	messageID := a.newID()
	_, err = a.db.Exec(`UPDATE intake_alerts SET message_id=?,sent_at=?,status='sending' WHERE event_id=? AND revision=?`, messageID, time.Now().UTC().Format(time.RFC3339Nano), in.EventID, in.Revision)
	if err != nil {
		return nil, err
	}
	id, err := a.send(ctx, jid, &waProto.Message{Conversation: proto.String(in.Text)}, messageID)
	if err != nil {
		_, updateErr := a.db.Exec(`UPDATE intake_alerts SET status='send_unknown' WHERE event_id=? AND revision=?`, in.EventID, in.Revision)
		if updateErr != nil {
			return nil, updateErr
		}
		return nil, fmt.Errorf("alert send unconfirmed")
	}
	_, err = a.db.Exec(`UPDATE intake_alerts SET message_id=?,sent_at=?,status='sent' WHERE event_id=? AND revision=?`, id, time.Now().UTC().Format(time.RFC3339Nano), in.EventID, in.Revision)
	if err != nil {
		return nil, err
	}
	return a.rows(in.EventID)
}

func (a *intakeAlerts) delete(ctx context.Context, eventID string) ([]intakeAlertRow, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	// Tombstones prevent delayed delivery of revisions already dismissed by TRB.
	if _, err := a.db.Exec(`INSERT OR IGNORE INTO intake_alert_closed_events VALUES(?)`, eventID); err != nil {
		return nil, err
	}
	rows, err := a.rows(eventID)
	if err != nil {
		return nil, err
	}
	for _, row := range rows {
		if _, err = a.db.Exec(`INSERT INTO intake_alert_closed VALUES(?,?) ON CONFLICT(request_key) DO UPDATE SET revision=max(revision,excluded.revision)`, row.RequestKey, row.Revision); err != nil {
			return nil, err
		}
	}
	if err = a.deleteRows(ctx, rows); err != nil {
		result, _ := a.rows(eventID)
		return result, err
	}
	return a.rows(eventID)
}

func (a *intakeAlerts) routes(mux *http.ServeMux) {
	a.cleanupRoute(mux)
	mux.HandleFunc("/api/intake-alerts", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
		defer cancel()
		var rows []intakeAlertRow
		var err error
		switch r.Method {
		case http.MethodPost:
			var in intakeAlertInput
			if !decodeIntake(w, r, &in) {
				return
			}
			rows, err = a.create(ctx, in)
		case http.MethodGet:
			eventID := r.URL.Query().Get("event_id")
			if !intakeID.MatchString(eventID) {
				http.Error(w, "Invalid event", 400)
				return
			}
			a.mu.Lock()
			rows, err = a.rows(eventID)
			a.mu.Unlock()
		default:
			http.Error(w, "Method not allowed", 405)
			return
		}
		if err != nil {
			http.Error(w, "Alert operation failed", 503)
			return
		}
		writeCapabilityJSON(w, 200, capabilityResponse{Success: true, Data: rows})
	})
	mux.HandleFunc("/api/intake-alerts/delete", func(w http.ResponseWriter, r *http.Request) {
		if !requireMethod(w, r, http.MethodPost) {
			return
		}
		var in struct {
			EventID string `json:"event_id"`
			Reason  string `json:"reason"`
		}
		if !decodeIntake(w, r, &in) {
			return
		}
		if !intakeID.MatchString(in.EventID) || len(in.Reason) > 32 {
			http.Error(w, "Invalid event", 400)
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
		defer cancel()
		rows, err := a.delete(ctx, in.EventID)
		if err != nil {
			writeCapabilityJSON(w, 200, capabilityResponse{Success: false, Data: rows, Message: "Alert deletion failed; inspect live rows and explicitly retry"})
			return
		}
		writeCapabilityJSON(w, 200, capabilityResponse{Success: true, Data: rows})
	})
}

func decodeIntake(w http.ResponseWriter, r *http.Request, target any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, 4096)
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if decoder.Decode(target) != nil {
		http.Error(w, "Invalid alert request", 400)
		return false
	}
	return true
}

func deleteForMePatch(jid types.JID, id string, sent time.Time) appstate.PatchInfo {
	return appstate.PatchInfo{Type: appstate.WAPatchRegularHigh, Mutations: []appstate.MutationInfo{{
		Index: []string{appstate.IndexDeleteMessageForMe, jid.String(), id, "1", "0"}, Version: 3,
		Value: &waSyncAction.SyncActionValue{DeleteMessageForMeAction: &waSyncAction.DeleteMessageForMeAction{DeleteMedia: proto.Bool(false), MessageTimestamp: proto.Int64(sent.Unix())}},
	}}}
}

// record persists the acknowledged revoke before app-state I/O so retries don't
// create another revoke stanza. Errors are fixed codes, never transport payloads.
func deleteIntakeMessage(ctx context.Context, client *whatsmeow.Client, send replySender, jid types.JID, id string, sent time.Time, apply func(context.Context, appstate.PatchInfo) error, record ...func(string, time.Time) error) (string, error) {
	revokeID, revokeErr := send(ctx, jid, client.BuildRevoke(jid, types.EmptyJID, id))
	mode := "delete_for_me_only"
	revokedAt := time.Now().UTC()
	if revokeErr == nil {
		mode = "revoke+delete_for_me"
	}
	if revokeErr != nil {
		revokeID = ""
		revokedAt = time.Time{}
	}
	for _, save := range record {
		if err := save(revokeID, revokedAt); err != nil {
			return mode, err
		}
	}
	var cleanupErr error
	if err := apply(ctx, deleteForMePatch(jid, id, sent)); err != nil {
		cleanupErr = errors.New("delete_for_me_original_failed")
	}
	if revokeErr == nil && revokeID != "" && revokeID != id {
		if err := apply(ctx, deleteForMePatch(jid, revokeID, revokedAt)); err != nil {
			cleanupErr = errors.Join(cleanupErr, errors.New("delete_for_me_revoke_failed"))
		}
	}
	if cleanupErr != nil && revokeErr != nil {
		cleanupErr = errors.Join(errors.New("revoke_failed"), cleanupErr)
	}
	return mode, cleanupErr
}

func registerIntakeAlerts(mux *http.ServeMux, client *whatsmeow.Client, s *MessageStore) error {
	if err := migrateIntakeAlerts(s.db); err != nil {
		return err
	}
	send := func(ctx context.Context, jid types.JID, msg *waProto.Message) (string, error) {
		if !client.IsConnected() || !client.IsLoggedIn() {
			return "", fmt.Errorf("bridge disconnected")
		}
		response, err := client.SendMessage(ctx, jid, msg)
		return response.ID, err
	}
	var a *intakeAlerts
	a = &intakeAlerts{db: s.db, apply: client.SendAppState, own: func() (types.JID, types.JID) {
		if client.Store.ID == nil {
			return types.EmptyJID, types.EmptyJID
		}
		return *client.Store.ID, client.Store.LID
	}, newID: client.GenerateMessageID, send: func(ctx context.Context, jid types.JID, msg *waProto.Message, id string) (string, error) {
		if !client.IsConnected() || !client.IsLoggedIn() {
			return "", fmt.Errorf("bridge disconnected")
		}
		response, err := client.SendMessage(ctx, jid, msg, whatsmeow.SendRequestExtra{ID: id})
		return response.ID, err
	}, remove: func(ctx context.Context, jid types.JID, id string, sent time.Time) (string, error) {
		return a.removeWithClient(ctx, client, send, jid, id, sent)
	}}
	a.routes(mux)
	return nil
}

func (a *intakeAlerts) removeWithClient(ctx context.Context, client *whatsmeow.Client, send replySender, jid types.JID, id string, sent time.Time) (string, error) {
	if _, err := a.ledgerTarget(jid.String(), id); err != nil {
		return "", err
	}
	// Only IDs associated with this ledger row may reach SendAppState.
	var revokedAt string
	if err := a.db.QueryRow(`SELECT revoked_at FROM intake_alerts WHERE chat_jid=? AND message_id=?`, jid.String(), id).Scan(&revokedAt); err != nil {
		return "", err
	}
	if revokedAt != "" {
		err := a.cleanupMessage(ctx, jid.String(), id, sent)
		if err == nil {
			_, err = a.db.Exec(`UPDATE intake_alerts SET delete_error='' WHERE chat_jid=? AND message_id=?`, jid.String(), id)
		}
		return "revoke+delete_for_me", err
	}
	return deleteIntakeMessage(ctx, client, send, jid, id, sent, a.applyGuarded, func(revokeID string, at time.Time) error {
		errorCode, timestamp := "", ""
		if at.IsZero() {
			errorCode = "revoke_failed"
		} else {
			timestamp = at.Format(time.RFC3339Nano)
		}
		_, err := a.db.Exec(`UPDATE intake_alerts SET revoke_message_id=?,revoked_at=?,delete_error=? WHERE chat_jid=? AND message_id=?`, revokeID, timestamp, errorCode, jid.String(), id)
		return err
	})
}
