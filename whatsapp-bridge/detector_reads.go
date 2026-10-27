package main

import (
	"context"
	"database/sql"
	"fmt"
	"net/http"
	"strconv"
	"strings"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types"
)

// Durable keys, not duplicate message bodies. Re-syncs of identical messages and
// receipt-only updates do not wake the detector. Bounded retention reports gaps.
func initMessageChanges(db *sql.DB) error {
	_, err := db.Exec(`CREATE TABLE IF NOT EXISTS message_changes (
		sequence INTEGER PRIMARY KEY AUTOINCREMENT, chat_jid TEXT NOT NULL, message_id TEXT NOT NULL);
		CREATE TRIGGER IF NOT EXISTS messages_change_insert AFTER INSERT ON messages BEGIN
		 INSERT INTO message_changes(chat_jid,message_id) VALUES(NEW.chat_jid,NEW.id);
		 DELETE FROM message_changes WHERE sequence <= (SELECT MAX(sequence)-100000 FROM message_changes);
		END;
		CREATE TRIGGER IF NOT EXISTS messages_change_update AFTER UPDATE OF content,media_type,filename,is_from_me ON messages
		WHEN NEW.content IS NOT OLD.content OR NEW.media_type IS NOT OLD.media_type
		 OR NEW.filename IS NOT OLD.filename OR NEW.is_from_me IS NOT OLD.is_from_me BEGIN
		 INSERT INTO message_changes(chat_jid,message_id) VALUES(NEW.chat_jid,NEW.id);
		 DELETE FROM message_changes WHERE sequence <= (SELECT MAX(sequence)-100000 FROM message_changes);
		END;`)
	return err
}

type aliasLookup interface {
	GetPNForLID(context.Context, types.JID) (types.JID, error)
	GetLIDForPN(context.Context, types.JID) (types.JID, error)
}

// Lookups happen on the HTTP reader goroutine, never inside WhatsApp's message
// callback, where blocking on the device store can starve transport keepalives.
func verifiedChatAliases(ctx context.Context, lookup aliasLookup, jid types.JID) []string {
	jid = jid.ToNonAD()
	aliases := []string{jid.String()}
	if lookup == nil {
		return aliases
	}
	var other types.JID
	var err error
	switch jid.Server {
	case types.DefaultUserServer:
		other, err = lookup.GetLIDForPN(ctx, jid)
		if other.Server != types.HiddenUserServer {
			return aliases
		}
	case types.HiddenUserServer:
		other, err = lookup.GetPNForLID(ctx, jid)
		if other.Server != types.DefaultUserServer {
			return aliases
		}
	default:
		return aliases
	}
	if err == nil && other.User != "" && other.String() != jid.String() {
		aliases = append(aliases, other.ToNonAD().String())
	}
	return aliases
}

type changedMessage struct {
	Sequence int64 `json:"sequence"`
	messageListItem
	ChatName string `json:"chat_name"`
	Deleted  bool   `json:"deleted"`
}

type messageChangesPage struct {
	Messages       []changedMessage `json:"messages"`
	NextSequence   int64            `json:"next_sequence"`
	MaxSequence    int64            `json:"max_sequence"`
	OldestSequence int64            `json:"oldest_sequence"`
	HasMore        bool             `json:"has_more"`
	GapDetected    bool             `json:"gap_detected"`
}

func (store *MessageStore) changesAfter(after int64, limit int) (*messageChangesPage, error) {
	page := &messageChangesPage{Messages: []changedMessage{}, NextSequence: after}
	tx, err := store.db.BeginTx(context.Background(), &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	if err = tx.QueryRow(`SELECT COALESCE(MAX(seq),0) FROM sqlite_sequence WHERE name='message_changes'`).Scan(&page.MaxSequence); err != nil {
		return nil, err
	}
	if err = tx.QueryRow(`SELECT COALESCE(MIN(sequence),0) FROM message_changes`).Scan(&page.OldestSequence); err != nil {
		return nil, err
	}
	page.GapDetected = after > page.MaxSequence || (page.OldestSequence > 0 && page.OldestSequence > after+1)
	rows, err := tx.Query(`SELECT e.sequence,e.message_id,e.chat_jid,COALESCE(m.sender,''),COALESCE(m.content,''),
		COALESCE(CAST(m.timestamp AS TEXT),''),COALESCE(m.is_from_me,0),COALESCE(m.media_type,''),
		COALESCE(m.filename,''),COALESCE(m.status,''),COALESCE(c.name,''),m.id IS NULL
		FROM message_changes e LEFT JOIN messages m ON m.chat_jid=e.chat_jid AND m.id=e.message_id
		LEFT JOIN chats c ON c.jid=e.chat_jid WHERE e.sequence>? ORDER BY e.sequence LIMIT ?`, after, limit+1)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var item changedMessage
		if err = rows.Scan(&item.Sequence, &item.ID, &item.ChatJID, &item.Sender, &item.Content, &item.Timestamp, &item.IsFromMe, &item.MediaType, &item.Filename, &item.Status, &item.ChatName, &item.Deleted); err != nil {
			return nil, err
		}
		if len(page.Messages) == limit {
			page.HasMore = true
			break
		}
		page.Messages = append(page.Messages, item)
		page.NextSequence = item.Sequence
	}
	if err = rows.Err(); err != nil {
		return nil, err
	}
	return page, nil
}

func registerDetectorReadHandlers(mux *http.ServeMux, client *whatsmeow.Client, store *MessageStore) {
	mux.HandleFunc("/api/message-changes", func(w http.ResponseWriter, r *http.Request) {
		if !requireMethod(w, r, http.MethodGet) {
			return
		}
		after, err := strconv.ParseInt(r.URL.Query().Get("after_sequence"), 10, 64)
		if err != nil || after < 0 {
			writeCapabilityError(w, 400, fmt.Errorf("nonnegative after_sequence required"))
			return
		}
		limit := 100
		if raw := r.URL.Query().Get("limit"); raw != "" {
			limit, err = strconv.Atoi(raw)
		}
		if err != nil || limit < 1 || limit > 200 {
			writeCapabilityError(w, 400, fmt.Errorf("limit must be 1..200"))
			return
		}
		page, err := store.changesAfter(after, limit)
		if err != nil {
			writeCapabilityError(w, 500, fmt.Errorf("change feed unavailable"))
			return
		}
		writeCapabilityJSON(w, 200, capabilityResponse{Success: true, Data: page})
	})
	mux.HandleFunc("/api/contact/aliases", func(w http.ResponseWriter, r *http.Request) {
		if !requireMethod(w, r, http.MethodGet) {
			return
		}
		jid, err := replyChat(strings.TrimSpace(r.URL.Query().Get("jid")))
		if err != nil {
			writeCapabilityError(w, 400, fmt.Errorf("exact chat JID required"))
			return
		}
		var lookup aliasLookup
		if client != nil && client.Store != nil {
			lookup = client.Store.LIDs
		}
		aliases := verifiedChatAliases(r.Context(), lookup, jid)
		phone := ""
		for _, a := range aliases {
			if strings.HasSuffix(a, "@s.whatsapp.net") {
				phone = strings.TrimSuffix(a, "@s.whatsapp.net")
			}
		}
		writeCapabilityJSON(w, 200, capabilityResponse{Success: true, Data: map[string]any{"jid": jid.String(), "aliases": aliases, "phone_number": phone}})
	})
}
