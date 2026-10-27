package main

import (
	"testing"
	"time"
)

func TestReceiptsPersistAndNeverRegress(t *testing.T) {
	store, err := NewMessageStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer store.db.Close()
	_, err = store.db.Exec(`INSERT INTO chats(jid) VALUES('15550000001@s.whatsapp.net');
		INSERT INTO messages(id,chat_jid,status) VALUES('synthetic-id','15550000001@s.whatsapp.net','sent')`)
	if err != nil {
		t.Fatal(err)
	}
	for _, status := range []string{"read", "delivered", "sent", "read"} {
		if err = store.StoreReceipt("15550000001@s.whatsapp.net", "synthetic-id", "15550000001@s.whatsapp.net", status, time.Unix(1000, 0)); err != nil {
			t.Fatal(err)
		}
	}
	var status string
	if err = store.db.QueryRow(`SELECT status FROM messages WHERE id='synthetic-id'`).Scan(&status); err != nil {
		t.Fatal(err)
	}
	if status != "read" {
		t.Fatalf("receipt regressed to %s", status)
	}
	var count int
	if err = store.db.QueryRow(`SELECT COUNT(*) FROM receipt_events`).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 3 {
		t.Fatalf("duplicate receipt retained: %d", count)
	}
	if err = store.StoreMessage("synthetic-id", "15550000001@s.whatsapp.net", "15550000001", "Synthetic resync", time.Unix(900, 0), true, "", "", "", nil, nil, nil, 0); err != nil {
		t.Fatal(err)
	}
	if err = store.db.QueryRow(`SELECT status FROM messages WHERE id='synthetic-id'`).Scan(&status); err != nil {
		t.Fatal(err)
	}
	if status != "read" {
		t.Fatalf("history sync regressed receipt to %s", status)
	}
}
