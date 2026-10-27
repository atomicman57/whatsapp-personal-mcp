package main

import (
	"fmt"
	"net/http"
	"strings"
	"unicode/utf8"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/appstate"
	"go.mau.fi/whatsmeow/proto/waSyncAction"
	"go.mau.fi/whatsmeow/types"
	"google.golang.org/protobuf/proto"
)

// Contact patches follow the protocol mapping used by Baileys' chat-utils.ts:
// contact / critical_unblock_low / API version 2. No delete operation is exposed.
func contactNamePatch(jid types.JID, name string) appstate.PatchInfo {
	return appstate.PatchInfo{Type: appstate.WAPatchCriticalUnblockLow,
		Mutations: []appstate.MutationInfo{{Index: []string{appstate.IndexContact, jid.String()}, Version: 2,
			Value: &waSyncAction.SyncActionValue{ContactAction: &waSyncAction.ContactAction{
				FullName: proto.String(name), FirstName: proto.String(name),
			}}}}}
}

func registerContactWorkflow(mux *http.ServeMux, client *whatsmeow.Client, store *MessageStore) {
	registerDeviceContactSearch(mux, client)
	mux.HandleFunc("/api/contact/details", func(w http.ResponseWriter, r *http.Request) {
		if !requireMethod(w, r, http.MethodGet) {
			return
		}
		value := strings.TrimSpace(r.URL.Query().Get("jid"))
		jid, err := parseUserJID(value)
		if err != nil || !exactPolicyRecipient.MatchString(value) || jid.Server == types.GroupServer {
			writeCapabilityError(w, 400, fmt.Errorf("exact phone number or user JID required"))
			return
		}
		if !client.IsConnected() {
			writeCapabilityError(w, 503, fmt.Errorf("WhatsApp offline"))
			return
		}
		contact, err := client.Store.Contacts.GetContact(r.Context(), jid)
		if err != nil {
			writeCapabilityError(w, 502, fmt.Errorf("contact lookup failed"))
			return
		}
		result := map[string]any{"jid": jid.String(), "saved_name": contact.FullName,
			"profile_name": contact.PushName, "business_name": contact.BusinessName,
			"is_business": contact.BusinessName != "", "about": nil, "photo_url": nil}
		if jid.Server == types.DefaultUserServer {
			result["phone_number"] = jid.User
		}
		if info, lookupErr := client.GetUserInfo(r.Context(), []types.JID{jid}); lookupErr == nil {
			if user, exists := info[jid]; exists {
				result["about"] = user.Status
				result["is_business"] = contact.BusinessName != "" || user.VerifiedName != nil
				if !user.LID.IsEmpty() {
					result["lid"] = user.LID.String()
				}
			}
		}
		if picture, pictureErr := client.GetProfilePictureInfo(r.Context(), jid, &whatsmeow.GetProfilePictureParams{}); pictureErr == nil && picture != nil {
			result["photo_url"] = picture.URL
		}
		writeCapabilityJSON(w, 200, capabilityResponse{Success: true, Data: result})
	})
	mux.HandleFunc("/api/contact/name", func(w http.ResponseWriter, r *http.Request) {
		if !requireMethod(w, r, http.MethodPost) {
			return
		}
		var request struct {
			PhoneNumber string `json:"phone_number"`
			Name        string `json:"name"`
		}
		if !decodeCapabilityRequest(w, r, &request) {
			return
		}
		jid, err := parseUserJID(request.PhoneNumber)
		name := strings.TrimSpace(request.Name)
		if err != nil || !exactPolicyRecipient.MatchString(strings.TrimSpace(request.PhoneNumber)) ||
			jid.Server != types.DefaultUserServer || name == "" || len(name) > 256 || !utf8.ValidString(name) {
			writeCapabilityError(w, 400, fmt.Errorf("exact phone number and nonempty name of at most 256 bytes required"))
			return
		}
		if !client.IsConnected() {
			writeCapabilityError(w, 503, fmt.Errorf("WhatsApp offline"))
			return
		}
		if err = client.SendAppState(r.Context(), contactNamePatch(jid, name)); err != nil {
			writeCapabilityError(w, 502, fmt.Errorf("contact sync failed"))
			return
		}
		if err = client.Store.Contacts.PutContactName(r.Context(), jid, name, name); err != nil {
			writeCapabilityError(w, 500, fmt.Errorf("contact synced; local name cache update failed"))
			return
		}
		_, err = store.db.Exec(`INSERT INTO chats(jid,name) VALUES(?,?) ON CONFLICT(jid) DO UPDATE SET name=excluded.name`, jid.String(), name)
		if err != nil {
			writeCapabilityError(w, 500, fmt.Errorf("contact synced; chat name cache update failed"))
			return
		}
		writeCapabilityJSON(w, 200, capabilityResponse{Success: true, Data: map[string]string{"jid": jid.String(), "name": name}})
	})
}
