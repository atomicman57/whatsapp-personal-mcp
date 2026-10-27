package main

import (
	"context"
	"fmt"
	"net/http"
	"sort"
	"strconv"
	"strings"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types"
)

type deviceContactResult struct {
	JID          string   `json:"jid"`
	Name         string   `json:"name"`
	SavedName    string   `json:"saved_name"`
	ProfileName  string   `json:"profile_name"`
	BusinessName string   `json:"business_name"`
	PhoneNumber  string   `json:"phone_number"`
	Aliases      []string `json:"aliases"`
}

// Device contacts are separate from message-cache chat labels. Search only
// local metadata; no network profile requests, message reads or state changes.
func matchingDeviceContacts(ctx context.Context, contacts map[types.JID]types.ContactInfo, lookup aliasLookup, query string, limit int) []deviceContactResult {
	query = strings.ToLower(strings.TrimSpace(query))
	result := []deviceContactResult{}
	if query == "" || len(query) > 256 || limit < 1 {
		return result
	}
	for jid, contact := range contacts {
		jid = jid.ToNonAD()
		if jid.Server != types.DefaultUserServer && jid.Server != types.HiddenUserServer {
			continue
		}
		fields := []string{contact.FullName, contact.FirstName, contact.PushName, contact.BusinessName, jid.String()}
		matched := false
		for _, field := range fields {
			if strings.Contains(strings.ToLower(field), query) {
				matched = true
				break
			}
		}
		if !matched {
			continue
		}
		name := ""
		for _, field := range []string{contact.FullName, contact.FirstName, contact.BusinessName, contact.PushName} {
			if strings.TrimSpace(field) != "" {
				name = strings.TrimSpace(field)
				break
			}
		}
		savedName := strings.TrimSpace(contact.FullName)
		if savedName == "" {
			savedName = strings.TrimSpace(contact.FirstName)
		}
		result = append(result, deviceContactResult{JID: jid.String(), Name: name, SavedName: savedName,
			ProfileName: contact.PushName, BusinessName: contact.BusinessName})
	}
	sort.Slice(result, func(i, j int) bool {
		a, b := strings.ToLower(result[i].Name), strings.ToLower(result[j].Name)
		if a == b {
			return result[i].JID < result[j].JID
		}
		return a < b
	})
	if len(result) > limit {
		result = result[:limit]
	}
	for i := range result {
		jid, _ := types.ParseJID(result[i].JID)
		result[i].Aliases = verifiedChatAliases(ctx, lookup, jid)
		for _, alias := range result[i].Aliases {
			if strings.HasSuffix(alias, "@s.whatsapp.net") {
				result[i].PhoneNumber = strings.TrimSuffix(alias, "@s.whatsapp.net")
			}
		}
	}
	return result
}

func registerDeviceContactSearch(mux *http.ServeMux, client *whatsmeow.Client) {
	mux.HandleFunc("/api/contacts/search", func(w http.ResponseWriter, r *http.Request) {
		if !requireMethod(w, r, http.MethodGet) {
			return
		}
		query := strings.TrimSpace(r.URL.Query().Get("query"))
		limit := 50
		var err error
		if value := r.URL.Query().Get("limit"); value != "" {
			limit, err = strconv.Atoi(value)
		}
		if query == "" || len(query) > 256 || err != nil || limit < 1 || limit > 100 {
			writeCapabilityError(w, 400, fmt.Errorf("nonempty query up to 256 bytes and limit 1..100 required"))
			return
		}
		if client == nil || client.Store == nil || client.Store.Contacts == nil {
			writeCapabilityError(w, 503, fmt.Errorf("device contacts unavailable"))
			return
		}
		contacts, err := client.Store.Contacts.GetAllContacts(r.Context())
		if err != nil {
			writeCapabilityError(w, 503, fmt.Errorf("device contacts unavailable"))
			return
		}
		matches := matchingDeviceContacts(r.Context(), contacts, client.Store.LIDs, query, limit)
		writeCapabilityJSON(w, 200, capabilityResponse{Success: true, Data: matches})
	})
}
