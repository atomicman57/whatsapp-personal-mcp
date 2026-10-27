package main

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"go.mau.fi/whatsmeow/types"
)

func TestDurableChangesSurviveRestartAndOnlyTrackContentChanges(t *testing.T) {
	dir := t.TempDir()
	s, err := NewMessageStore(dir)
	if err != nil {
		t.Fatal(err)
	}
	chat := "15550000001@s.whatsapp.net"
	if err = s.StoreChat(chat, "Synthetic", time.Now()); err != nil {
		t.Fatal(err)
	}
	put := func(id, content string) {
		t.Helper()
		if err = s.StoreMessage(id, chat, "15550000001", content, time.Unix(1000, 0), false, "", "", "", nil, nil, nil, 0); err != nil {
			t.Fatal(err)
		}
	}
	put("one", "Please send NGN 5000")
	put("one", "Please send NGN 5000")
	if err = s.SetMessageStatus(chat, "one", "read"); err != nil {
		t.Fatal(err)
	}
	page, err := s.changesAfter(0, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(page.Messages) != 1 || page.MaxSequence != 1 {
		t.Fatalf("unchanged replay/receipt woke detector: %+v", page)
	}
	put("one", "Please send NGN 6000")
	put("two", "Late historical body")
	page, err = s.changesAfter(0, 1)
	if err != nil {
		t.Fatal(err)
	}
	if len(page.Messages) != 1 || !page.HasMore || page.NextSequence != 1 || page.Messages[0].Content != "Please send NGN 6000" {
		t.Fatalf("bad first page: %+v", page)
	}
	if err = s.Close(); err != nil {
		t.Fatal(err)
	}
	s, err = NewMessageStore(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	page, err = s.changesAfter(1, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(page.Messages) != 2 || page.MaxSequence != 3 || page.HasMore {
		t.Fatalf("restart lost changes: %+v", page)
	}
	if _, err = s.db.Exec("DELETE FROM message_changes WHERE sequence<3"); err != nil {
		t.Fatal(err)
	}
	page, err = s.changesAfter(1, 10)
	if err != nil || !page.GapDetected {
		t.Fatal("retention gap hidden", err)
	}
	page, err = s.changesAfter(4, 10)
	if err != nil || !page.GapDetected {
		t.Fatal("restored/future cursor hidden", err)
	}
}

type syntheticAliases struct {
	mapped types.JID
	err    error
}

func (s syntheticAliases) GetPNForLID(context.Context, types.JID) (types.JID, error) {
	return s.mapped, s.err
}
func (s syntheticAliases) GetLIDForPN(context.Context, types.JID) (types.JID, error) {
	return s.mapped, s.err
}

func TestAliasesRequireActualMappingAndPreserveNamespaces(t *testing.T) {
	ctx := context.Background()
	pn := types.NewJID("15550000001", types.DefaultUserServer)
	lid := types.NewJID("99900001", types.HiddenUserServer)
	if got := verifiedChatAliases(ctx, syntheticAliases{mapped: lid}, pn); len(got) != 2 || got[1] != lid.String() {
		t.Fatal(got)
	}
	if got := verifiedChatAliases(ctx, syntheticAliases{mapped: pn}, lid); len(got) != 2 || got[1] != pn.String() {
		t.Fatal(got)
	}
	for _, lookup := range []aliasLookup{nil, syntheticAliases{mapped: pn}, syntheticAliases{mapped: lid, err: fmt.Errorf("unavailable")}} {
		if got := verifiedChatAliases(ctx, lookup, pn); len(got) != 1 {
			t.Fatal("unverified alias accepted", got)
		}
	}
	group := types.NewJID("123456789", types.GroupServer)
	if got := verifiedChatAliases(ctx, syntheticAliases{mapped: pn}, group); len(got) != 1 {
		t.Fatal("group expanded into person", got)
	}
}

func TestDetectorReadRoutesValidateBounds(t *testing.T) {
	s, err := NewMessageStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	mux := http.NewServeMux()
	registerDetectorReadHandlers(mux, nil, s)
	for path, want := range map[string]int{
		"/api/message-changes?after_sequence=0":               200,
		"/api/message-changes?after_sequence=-1":              400,
		"/api/message-changes?after_sequence=0&limit=201":     400,
		"/api/contact/aliases?jid=15550000001@s.whatsapp.net": 200,
		"/api/contact/aliases?jid=NotAContact":                400,
	} {
		w := httptest.NewRecorder()
		mux.ServeHTTP(w, httptest.NewRequest("GET", path, nil))
		if w.Code != want {
			t.Fatalf("%s: %d", path, w.Code)
		}
	}
}
