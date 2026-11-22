package main

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"time"

	"go.mau.fi/whatsmeow/appstate"
	"go.mau.fi/whatsmeow/types"
)

// The transport boundary checks the database, not caller-supplied row metadata.
func (a *intakeAlerts) ledgerTarget(chat, id string) (types.JID, error) {
	jid, err := a.self(chat)
	if err != nil {
		return types.EmptyJID, err
	}
	var count int
	err = a.db.QueryRow(`SELECT count(*) FROM intake_alerts WHERE chat_jid=? AND message_id<>'' AND (message_id=? OR (revoke_message_id<>'' AND revoke_message_id=?))`, chat, id, id).Scan(&count)
	if err != nil {
		return types.EmptyJID, err
	}
	if id == "" || count == 0 {
		return types.EmptyJID, fmt.Errorf("ledger message required")
	}
	return jid, nil
}

func (a *intakeAlerts) applyGuarded(ctx context.Context, patch appstate.PatchInfo) error {
	for _, m := range patch.Mutations {
		if len(m.Index) != 5 || m.Index[0] != appstate.IndexDeleteMessageForMe {
			return fmt.Errorf("invalid cleanup patch")
		}
		if _, err := a.ledgerTarget(m.Index[1], m.Index[2]); err != nil {
			return err
		}
	}
	return a.apply(ctx, patch)
}

func (a *intakeAlerts) cleanupMessage(ctx context.Context, chat, id string, sent time.Time) error {
	jid, err := a.ledgerTarget(chat, id)
	if err != nil {
		return err
	}
	var revokeID, revokedAt string
	if err = a.db.QueryRow(`SELECT revoke_message_id,revoked_at FROM intake_alerts WHERE chat_jid=? AND message_id=?`, chat, id).Scan(&revokeID, &revokedAt); err != nil {
		return err
	}
	var cleanupErr error
	if err = a.applyGuarded(ctx, deleteForMePatch(jid, id, sent)); err != nil {
		cleanupErr = errors.New("delete_for_me_original_failed")
	}
	if revokeID != "" && revokeID != id {
		at, parseErr := time.Parse(time.RFC3339Nano, revokedAt)
		if parseErr != nil {
			return errors.Join(cleanupErr, errors.New("invalid_revoke_timestamp"))
		}
		if err = a.applyGuarded(ctx, deleteForMePatch(jid, revokeID, at)); err != nil {
			cleanupErr = errors.Join(cleanupErr, errors.New("delete_for_me_revoke_failed"))
		}
	}
	return cleanupErr
}

type intakeCleanupResult struct {
	DryRun  bool             `json:"dry_run"`
	Count   int              `json:"count"`
	Cleaned int              `json:"cleaned"`
	Rows    []intakeAlertRow `json:"rows"`
}

func (a *intakeAlerts) cleanup(ctx context.Context, since time.Time, dryRun bool) (intakeCleanupResult, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	result := intakeCleanupResult{DryRun: dryRun, Rows: []intakeAlertRow{}}
	rows, err := a.db.Query(`SELECT event_id,request_key,revision,message_id,chat_jid,sent_at,deleted_at,delete_mode,status,revoke_message_id,revoked_at,cleanup_at,delete_error FROM intake_alerts WHERE message_id<>'' AND cleanup_at='' AND (deleted_at<>'' OR revoked_at<>'' OR status IN ('deleted','revoked')) ORDER BY sent_at,event_id,revision`)
	if err != nil {
		return result, err
	}
	for rows.Next() {
		var row intakeAlertRow
		if err = rows.Scan(&row.EventID, &row.RequestKey, &row.Revision, &row.MessageID, &row.ChatJID, &row.SentAt, &row.DeletedAt, &row.DeleteMode, &row.Status, &row.RevokeID, &row.RevokedAt, &row.CleanupAt, &row.DeleteError); err != nil {
			break
		}
		sent, parseErr := time.Parse(time.RFC3339Nano, row.SentAt)
		if parseErr != nil {
			err = fmt.Errorf("invalid alert timestamp")
			break
		}
		if sent.Before(since) {
			continue
		}
		if _, err = a.self(row.ChatJID); err != nil {
			break
		}
		result.Rows = append(result.Rows, row)
	}
	scanErr := rows.Err()
	_ = rows.Close()
	if err != nil {
		return result, err
	}
	if scanErr != nil {
		return result, scanErr
	}
	result.Count = len(result.Rows)
	if dryRun {
		return result, nil
	}
	var operationErr error
	for i := range result.Rows {
		row := &result.Rows[i]
		sent, _ := time.Parse(time.RFC3339Nano, row.SentAt)
		cleanupErr := a.cleanupMessage(ctx, row.ChatJID, row.MessageID, sent)
		if cleanupErr != nil {
			row.DeleteError = cleanupErr.Error()
			operationErr = fmt.Errorf("alert cleanup failed")
		} else {
			row.CleanupAt = time.Now().UTC().Format(time.RFC3339Nano)
			if row.DeleteError != "revoke_failed" {
				row.DeleteError = ""
			}
			if row.DeletedAt == "" {
				row.DeletedAt = row.CleanupAt
			}
			row.Status = "deleted"
			if row.RevokedAt != "" || row.DeleteMode == "everyone" {
				row.DeleteMode = "revoke+delete_for_me"
			} else {
				row.DeleteMode = "delete_for_me_only"
			}
			result.Cleaned++
		}
		if _, err = a.db.Exec(`UPDATE intake_alerts SET cleanup_at=?,delete_error=?,deleted_at=?,status=?,delete_mode=? WHERE event_id=? AND revision=?`, row.CleanupAt, row.DeleteError, row.DeletedAt, row.Status, row.DeleteMode, row.EventID, row.Revision); err != nil {
			return result, err
		}
	}
	return result, operationErr
}

func (a *intakeAlerts) cleanupRoute(mux *http.ServeMux) {
	mux.HandleFunc("/api/intake-alerts/cleanup", func(w http.ResponseWriter, r *http.Request) {
		if !requireMethod(w, r, http.MethodPost) {
			return
		}
		in := struct {
			Since  string `json:"since"`
			DryRun bool   `json:"dry_run"`
		}{DryRun: true}
		if !decodeIntake(w, r, &in) {
			return
		}
		var since time.Time
		var err error
		if in.Since != "" {
			since, err = time.Parse(time.RFC3339Nano, in.Since)
			if err != nil {
				http.Error(w, "since must be an ISO timestamp with timezone", 400)
				return
			}
		}
		ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
		defer cancel()
		result, err := a.cleanup(ctx, since, in.DryRun)
		message := ""
		if err != nil {
			message = "Alert cleanup failed; inspect ledger and explicitly retry"
		}
		writeCapabilityJSON(w, 200, capabilityResponse{Success: err == nil, Data: result, Message: message})
	})
}
