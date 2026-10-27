package main

import (
	"bytes"
	"crypto/subtle"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"regexp"
	"strings"
)

var exactPolicyRecipient = regexp.MustCompile(`^(?:\+?[1-9][0-9]{5,14}|[1-9][0-9]{5,14}@s\.whatsapp\.net|[0-9]{6,20}@lid|[0-9]{6,20}(?:-[0-9]{6,12})?@g\.us)$`)

// All bridge routes require the deployment token, including direct REST calls.
// Send access is enabled once in deployment configuration, never by a tool call.
func protectBridgeAccess(next http.Handler, lookups ...recipientLookup) http.Handler {
	var lookup recipientLookup
	if len(lookups) > 0 {
		lookup = lookups[0]
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		token := os.Getenv("WHATSAPP_BRIDGE_TOKEN")
		if len(token) < 32 {
			http.Error(w, "Bridge authentication is not configured", http.StatusServiceUnavailable)
			return
		}
		provided := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
		readToken := os.Getenv("WHATSAPP_BRIDGE_READ_TOKEN")
		fullAccess := subtle.ConstantTimeCompare([]byte(provided), []byte(token)) == 1
		readAccess := len(readToken) >= 32 && subtle.ConstantTimeCompare([]byte(provided), []byte(readToken)) == 1
		if len(r.Header.Values("Authorization")) != 1 || !strings.HasPrefix(r.Header.Get("Authorization"), "Bearer ") || (!fullAccess && !readAccess) {
			http.Error(w, "Unauthorized", http.StatusUnauthorized)
			return
		}
		if !fullAccess && r.Method != http.MethodGet && r.Method != http.MethodHead {
			http.Error(w, "Read-only credential", http.StatusForbidden)
			return
		}
		w.Header().Set("Cache-Control", "no-store")
		if r.Method == http.MethodPost && (r.URL.Path == "/api/send" || r.URL.Path == "/api/reply") {
			if !strings.EqualFold(os.Getenv("WHATSAPP_SEND_ENABLED"), "true") {
				http.Error(w, "Sending disabled", http.StatusForbidden)
				return
			}
			body, err := io.ReadAll(r.Body)
			if err != nil {
				http.Error(w, "Invalid body", http.StatusBadRequest)
				return
			}
			r.Body = io.NopCloser(bytes.NewReader(body))
			var input struct {
				Recipient string `json:"recipient"`
				ChatJID   string `json:"chat_jid"`
			}
			if json.Unmarshal(body, &input) != nil {
				http.Error(w, "Invalid body", http.StatusBadRequest)
				return
			}
			target := input.Recipient
			if target == "" {
				target = input.ChatJID
			}
			jid, err := replyChat(target)
			if err != nil || !exactPolicyRecipient.MatchString(strings.TrimSpace(target)) {
				http.Error(w, "Exact recipient required", http.StatusBadRequest)
				return
			}
			if status, reason := recipientAccess(r.Context(), jid, lookup); status != 0 {
				http.Error(w, reason, status)
				return
			}
		} else if r.Method != http.MethodGet && r.Method != http.MethodHead &&
			r.URL.Path == "/api/contact/name" && strings.EqualFold(os.Getenv("WHATSAPP_SEND_SAVED_CONTACTS"), "true") {
			http.Error(w, "Contact changes disabled in saved-contact sending mode", http.StatusForbidden)
			return
		} else if r.Method != http.MethodGet && r.Method != http.MethodHead &&
			r.URL.Path != "/api/download" && r.URL.Path != "/api/contacts/is-on-whatsapp" &&
			!strings.EqualFold(os.Getenv("WHATSAPP_MUTATIONS_ENABLED"), "true") {
			http.Error(w, "Mutations disabled", http.StatusForbidden)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func policyRecipients(name string) (map[string]bool, bool) {
	result := map[string]bool{}
	for _, value := range strings.Split(os.Getenv(name), ",") {
		if strings.TrimSpace(value) == "" {
			continue
		}
		jid, err := replyChat(strings.TrimSpace(value))
		if err != nil || !exactPolicyRecipient.MatchString(strings.TrimSpace(value)) {
			return nil, false
		}
		result[jid.String()] = true
	}
	return result, true
}
