package main

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"strings"
	"unicode"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types"
)

type recipientIdentity struct {
	Aliases []string
	Names   []string
	Saved   bool
}

type recipientLookup func(context.Context, types.JID) (recipientIdentity, error)

func normalizedPolicyName(value string) string {
	return strings.Map(func(r rune) rune {
		if unicode.IsLetter(r) || unicode.IsDigit(r) {
			return unicode.ToLower(r)
		}
		return -1
	}, value)
}

func deviceRecipientLookup(client *whatsmeow.Client) recipientLookup {
	return func(ctx context.Context, jid types.JID) (recipientIdentity, error) {
		if client == nil || client.Store == nil || client.Store.Contacts == nil {
			return recipientIdentity{}, fmt.Errorf("recipient directory unavailable")
		}
		identity := recipientIdentity{Aliases: verifiedChatAliases(ctx, client.Store.LIDs, jid)}
		for _, value := range identity.Aliases {
			alias, err := types.ParseJID(value)
			if err != nil {
				return recipientIdentity{}, fmt.Errorf("recipient directory unavailable")
			}
			contact, err := client.Store.Contacts.GetContact(ctx, alias)
			if err != nil {
				return recipientIdentity{}, fmt.Errorf("recipient directory unavailable")
			}
			identity.Names = append(identity.Names, contact.FullName, contact.FirstName, contact.PushName, contact.BusinessName)
			// Profile/business names are sender-controlled and cannot grant access.
			for _, name := range []string{contact.FullName, contact.FirstName} {
				if strings.ContainsFunc(name, unicode.IsLetter) {
					identity.Saved = true
				}
			}
		}
		return identity, nil
	}
}

func recipientAccess(ctx context.Context, jid types.JID, lookup recipientLookup) (int, string) {
	allowed, validAllowlist := policyRecipients("WHATSAPP_SEND_ALLOWLIST")
	denied, validDenylist := policyRecipients("WHATSAPP_DO_NOT_CONTACT")
	if !validAllowlist || !validDenylist || denied[jid.String()] {
		return 403, "Recipient excluded or policy invalid"
	}
	savedEnabled := strings.EqualFold(os.Getenv("WHATSAPP_SEND_SAVED_CONTACTS"), "true")
	blockedNames := []string{}
	if raw := os.Getenv("WHATSAPP_DO_NOT_CONTACT_NAMES"); raw != "" {
		for _, value := range strings.Split(raw, ",") {
			value = normalizedPolicyName(value)
			if value == "" {
				return 403, "Recipient policy invalid"
			}
			blockedNames = append(blockedNames, value)
		}
	}
	identity := recipientIdentity{Aliases: []string{jid.String()}}
	if lookup != nil {
		var err error
		identity, err = lookup(ctx, jid)
		if err != nil {
			return 503, "Recipient directory unavailable"
		}
	} else if savedEnabled || len(blockedNames) > 0 {
		return 503, "Recipient directory unavailable"
	}
	explicit, phoneKnown := allowed[jid.String()], jid.Server == types.DefaultUserServer
	for _, alias := range identity.Aliases {
		if denied[alias] {
			return 403, "Recipient excluded"
		}
		explicit = explicit || allowed[alias]
		phoneKnown = phoneKnown || strings.HasSuffix(alias, "@s.whatsapp.net")
	}
	for _, name := range identity.Names {
		name = normalizedPolicyName(name)
		for _, blocked := range blockedNames {
			if strings.Contains(name, blocked) {
				return 403, "Recipient excluded"
			}
		}
	}
	savedAllowed := savedEnabled && identity.Saved && phoneKnown &&
		(jid.Server == types.DefaultUserServer || jid.Server == types.HiddenUserServer)
	if len(allowed) > 0 && !explicit && !savedAllowed {
		return 403, "Recipient is not an authorized saved contact"
	}
	if savedEnabled && !explicit && !savedAllowed {
		return 403, "Recipient is not an authorized saved contact"
	}
	if len(denied) > 0 && !phoneKnown && !explicit {
		return 403, "Recipient identity unresolved"
	}
	return 0, "allowed"
}

func registerRecipientPolicyRead(mux *http.ServeMux, lookup recipientLookup) {
	mux.HandleFunc("/api/recipient/policy", func(w http.ResponseWriter, r *http.Request) {
		if !requireMethod(w, r, http.MethodGet) {
			return
		}
		value := strings.TrimSpace(r.URL.Query().Get("recipient"))
		jid, err := replyChat(value)
		if err != nil || !exactPolicyRecipient.MatchString(value) {
			writeCapabilityError(w, 400, fmt.Errorf("exact recipient required"))
			return
		}
		status, reason := recipientAccess(r.Context(), jid, lookup)
		if status >= 500 {
			writeCapabilityError(w, status, fmt.Errorf("recipient directory unavailable"))
			return
		}
		enabled := strings.EqualFold(os.Getenv("WHATSAPP_SEND_ENABLED"), "true")
		if !enabled {
			reason = "Sending disabled"
		}
		writeCapabilityJSON(w, 200, capabilityResponse{Success: true, Data: map[string]any{
			"allowed": enabled && status == 0, "reason": reason, "send_enabled": enabled, "read_only": true}})
	})
}
