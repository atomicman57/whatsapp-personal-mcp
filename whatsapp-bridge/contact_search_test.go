package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"go.mau.fi/whatsmeow/types"
)

func TestDeviceContactSearchFindsSavedProfileAndBusinessNames(t *testing.T) {
	pn := types.NewJID("15550000001", types.DefaultUserServer)
	lid := types.NewJID("99887766", types.HiddenUserServer)
	contacts := map[types.JID]types.ContactInfo{
		pn:                                       {FullName: "Housekeeper Example", PushName: "Different Profile"},
		lid:                                      {BusinessName: "Example Networks", PushName: "Seller Profile"},
		types.NewJID("12345", types.GroupServer): {FullName: "Example Group"},
	}
	for _, query := range []string{"Housekeeper", "different PROFILE", "15550000001"} {
		got := matchingDeviceContacts(context.Background(), contacts, nil, query, 50)
		if len(got) != 1 || got[0].Name != "Housekeeper Example" || got[0].PhoneNumber != pn.User {
			t.Fatalf("saved/profile/phone discovery failed: %+v", got)
		}
	}
	got := matchingDeviceContacts(context.Background(), contacts, nil, "Example", 50)
	if len(got) != 2 || got[0].Name != "Example Networks" || got[0].PhoneNumber != "" {
		t.Fatalf("group filtering, sort or opaque LID failed: %+v", got)
	}
	mapped := matchingDeviceContacts(context.Background(), contacts, syntheticAliases{mapped: pn}, "Networks", 50)
	if len(mapped) != 1 || mapped[0].PhoneNumber != pn.User || len(mapped[0].Aliases) != 2 {
		t.Fatalf("verified LID mapping missing: %+v", mapped)
	}
	if got := matchingDeviceContacts(context.Background(), contacts, nil, "Example", 1); len(got) != 1 {
		t.Fatal("contact search limit ignored")
	}
	if got := matchingDeviceContacts(context.Background(), contacts, nil, " ", 50); len(got) != 0 {
		t.Fatal("empty query exposed directory")
	}
}

func TestDeviceContactSearchValidatesBeforeAccessingStore(t *testing.T) {
	mux := http.NewServeMux()
	registerDeviceContactSearch(mux, nil)
	for _, target := range []string{"/api/contacts/search", "/api/contacts/search?query=Example&limit=0", "/api/contacts/search?query=Example&limit=101"} {
		r := httptest.NewRecorder()
		mux.ServeHTTP(r, httptest.NewRequest(http.MethodGet, target, nil))
		if r.Code != 400 {
			t.Fatalf("invalid search returned %d", r.Code)
		}
	}
	r := httptest.NewRecorder()
	mux.ServeHTTP(r, httptest.NewRequest(http.MethodGet, "/api/contacts/search?query=Example", nil))
	if r.Code != 503 {
		t.Fatal("missing device store should be unavailable")
	}
	r = httptest.NewRecorder()
	mux.ServeHTTP(r, httptest.NewRequest(http.MethodPost, "/api/contacts/search?query=Example", nil))
	if r.Code != 405 {
		t.Fatal("contact directory must remain read-only")
	}
}
