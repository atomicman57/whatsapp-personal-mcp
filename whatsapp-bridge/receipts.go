package main

import "time"

func (store *MessageStore) StoreReceipt(chat, messageID, recipient, status string, timestamp time.Time) error {
	tx, err := store.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	_, err = tx.Exec(`INSERT OR IGNORE INTO receipt_events(chat_jid,message_id,recipient_jid,status,timestamp) VALUES(?,?,?,?,?)`,
		chat, messageID, recipient, status, timestamp.UTC().Format(time.RFC3339Nano))
	if err != nil {
		return err
	}
	_, err = tx.Exec(`UPDATE messages SET status=? WHERE chat_jid=? AND id=? AND
		CASE status WHEN 'sent' THEN 1 WHEN 'delivered' THEN 2 WHEN 'read' THEN 3 WHEN 'played' THEN 4 ELSE 0 END <
		CASE ? WHEN 'sent' THEN 1 WHEN 'delivered' THEN 2 WHEN 'read' THEN 3 WHEN 'played' THEN 4 ELSE 0 END`,
		status, chat, messageID, status)
	if err != nil {
		return err
	}
	return tx.Commit()
}
