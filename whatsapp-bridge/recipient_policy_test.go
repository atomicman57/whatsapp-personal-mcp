package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
)

type policyTestContacts struct {
	*store.NoopStore
	contact types.ContactInfo
}

func (s policyTestContacts) GetContact(context.Context, types.JID) (types.ContactInfo, error) {
	return s.contact, nil
}

func TestOnlyOwnerSavedNamesGrantDirectoryAccess(t *testing.T) {
	for _, c := range []struct {
		contact types.ContactInfo
		saved   bool
	}{
		{types.ContactInfo{PushName: "Familiar Name", BusinessName: "Trusted Business"}, false},
		{types.ContactInfo{FullName: "15550000001"}, false},
		{types.ContactInfo{FullName: "Saved Person"}, true},
		{types.ContactInfo{FirstName: "Saved First Name"}, true},
	} {
		client := &whatsmeow.Client{Store: &store.Device{Contacts: policyTestContacts{contact: c.contact}, LIDs: &store.NoopStore{}}}
		got, err := deviceRecipientLookup(client)(context.Background(), types.NewJID("15550000001", types.DefaultUserServer))
		if err != nil || got.Saved != c.saved {
			t.Fatalf("saved-name decision: got %+v, error %v", got, err)
		}
	}
}

func TestSavedRecipientPolicyDenialsTakePrecedenceAndAliasesStayVerified(t *testing.T) {
	phone, lid := "15550000001@s.whatsapp.net", "99887766@lid"
	for _, c := range []struct {
		name, target, denied string
		identity             recipientIdentity
		err                  bool
		want                 int
	}{
		{"saved phone", phone, "", recipientIdentity{Aliases: []string{phone}, Saved: true, Names: []string{"Saved Person"}}, false, 204},
		{"saved mapped LID", lid, "", recipientIdentity{Aliases: []string{lid, phone}, Saved: true}, false, 204},
		{"profile cannot grant access", phone, "", recipientIdentity{Aliases: []string{phone}, Names: []string{"Familiar Profile"}}, false, 403},
		{"unmapped saved LID", lid, "", recipientIdentity{Aliases: []string{lid}, Saved: true}, false, 403},
		{"blocked phone behind LID", lid, phone, recipientIdentity{Aliases: []string{lid, phone}, Saved: true}, false, 403},
		{"normalized seller name", phone, "", recipientIdentity{Aliases: []string{phone}, Saved: true, Names: []string{"DEMO-Vendor Networks"}}, false, 403},
		{"profile seller name overrides saved", phone, "", recipientIdentity{Aliases: []string{phone}, Saved: true, Names: []string{"Known Contact", "Other_Vendor"}}, false, 403},
		{"directory failure", phone, "", recipientIdentity{}, true, 503},
		{"group not a saved direct contact", "99887766@g.us", "", recipientIdentity{Aliases: []string{"99887766@g.us"}, Saved: true}, false, 403},
	} {
		t.Run(c.name, func(t *testing.T) {
			t.Setenv("WHATSAPP_BRIDGE_TOKEN", strings.Repeat("w", 32))
			t.Setenv("WHATSAPP_SEND_ENABLED", "true")
			t.Setenv("WHATSAPP_SEND_SAVED_CONTACTS", "true")
			t.Setenv("WHATSAPP_SEND_ALLOWLIST", "15550000009")
			t.Setenv("WHATSAPP_DO_NOT_CONTACT", c.denied)
			t.Setenv("WHATSAPP_DO_NOT_CONTACT_NAMES", "DemoVendor,Other Vendor")
			lookup := func(context.Context, types.JID) (recipientIdentity, error) {
				if c.err {
					return recipientIdentity{}, fmt.Errorf("synthetic unavailable")
				}
				return c.identity, nil
			}
			h := protectBridgeAccess(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(204) }), lookup)
			for _, path := range []string{"/api/send", "/api/reply"} {
				body, _ := json.Marshal(map[string]any{"recipient": c.target, "saved": true, "name": "Spoofed saved contact"})
				r := httptest.NewRequest(http.MethodPost, path, strings.NewReader(string(body)))
				r.Header.Set("Authorization", "Bearer "+strings.Repeat("w", 32))
				w := httptest.NewRecorder()
				h.ServeHTTP(w, r)
				if w.Code != c.want {
					t.Fatalf("%s: got %d want %d", path, w.Code, c.want)
				}
			}
		})
	}
}

func TestRecipientPolicyReadNeverDispatchesAndExplicitOwnerStillWorks(t *testing.T) {
	t.Setenv("WHATSAPP_SEND_ENABLED", "true")
	t.Setenv("WHATSAPP_SEND_SAVED_CONTACTS", "true")
	t.Setenv("WHATSAPP_DO_NOT_CONTACT_NAMES", "DemoVendor")
	t.Setenv("WHATSAPP_SEND_ALLOWLIST", "15550000001")
	t.Setenv("WHATSAPP_DO_NOT_CONTACT", "")
	lookup := func(ctx context.Context, jid types.JID) (recipientIdentity, error) {
		return recipientIdentity{Aliases: []string{jid.String()}}, nil
	}
	mux := http.NewServeMux()
	registerRecipientPolicyRead(mux, lookup)
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/recipient/policy?recipient=15550000001", nil))
	var response struct {
		Data struct {
			Allowed  bool `json:"allowed"`
			ReadOnly bool `json:"read_only"`
		} `json:"data"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &response); err != nil || w.Code != 200 || !response.Data.Allowed || !response.Data.ReadOnly {
		t.Fatal("owner exception/read-only policy result incorrect", err, w.Code)
	}
	blockedOwner := func(ctx context.Context, jid types.JID) (recipientIdentity, error) {
		return recipientIdentity{Aliases: []string{jid.String()}, Names: []string{"Demo Vendor"}}, nil
	}
	if status, _ := recipientAccess(context.Background(), types.NewJID("15550000001", types.DefaultUserServer), blockedOwner); status != 403 {
		t.Fatal("explicit exception must not override excluded name")
	}
	t.Setenv("WHATSAPP_DO_NOT_CONTACT_NAMES", ",")
	if status, _ := recipientAccess(context.Background(), types.NewJID("15550000001", types.DefaultUserServer), lookup); status != 403 {
		t.Fatal("invalid name policy must fail closed")
	}
}

func TestSavedContactModeCannotGrantItselfAccessByEditingContacts(t *testing.T) {
	t.Setenv("WHATSAPP_BRIDGE_TOKEN", strings.Repeat("w", 32))
	t.Setenv("WHATSAPP_SEND_SAVED_CONTACTS", "true")
	t.Setenv("WHATSAPP_MUTATIONS_ENABLED", "true")
	h := protectBridgeAccess(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Fatal("contact mutation reached handler in saved-contact mode")
	}))
	r := httptest.NewRequest(http.MethodPost, "/api/contact/name", strings.NewReader(`{"phone_number":"15550000001","name":"Saved Person"}`))
	r.Header.Set("Authorization", "Bearer "+strings.Repeat("w", 32))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != 403 {
		t.Fatal("agent could save a contact to authorize its own sends")
	}
}
