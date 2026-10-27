package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestBridgeAccessGuardsDirectRESTCalls(t *testing.T) {
	token := strings.Repeat("a", 32)
	cases := []struct {
		name, token, authorization, path, body, send, mutations, denied, allowed string
		want                                                                     int
	}{
		{name: "unconfigured", want: 503},
		{name: "missing token", token: token, want: 401},
		{name: "bare token", token: token, authorization: token, want: 401},
		{name: "read authorized", token: token, authorization: "Bearer " + token, want: 204},
		{name: "send disabled", path: "/api/send", token: token, authorization: "Bearer " + token, want: 403},
		{name: "direct send", path: "/api/send", body: `{"recipient":"15550000001","message":"Synthetic"}`, send: "true", token: token, authorization: "Bearer " + token, want: 204},
		{name: "deny phone alias", path: "/api/send", body: `{"recipient":"15550000001@s.whatsapp.net"}`, send: "true", denied: "+15550000001", token: token, authorization: "Bearer " + token, want: 403},
		{name: "deny name", path: "/api/send", body: `{"recipient":"Person15550000001"}`, send: "true", token: token, authorization: "Bearer " + token, want: 400},
		{name: "reply denied", path: "/api/reply", body: `{"chat_jid":"15550000001@s.whatsapp.net"}`, send: "true", denied: "15550000001", token: token, authorization: "Bearer " + token, want: 403},
		{name: "allowlist refusal", path: "/api/send", body: `{"recipient":"15550000001"}`, send: "true", allowed: "15550000002", token: token, authorization: "Bearer " + token, want: 403},
		{name: "unresolved lid", path: "/api/send", body: `{"recipient":"99900001@lid"}`, send: "true", denied: "15550000002", token: token, authorization: "Bearer " + token, want: 403},
		{name: "mutations disabled", path: "/api/groups/create", token: token, authorization: "Bearer " + token, want: 403},
		{name: "mutations enabled", path: "/api/groups/create", mutations: "true", token: token, authorization: "Bearer " + token, want: 204},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Setenv("WHATSAPP_BRIDGE_TOKEN", c.token)
			t.Setenv("WHATSAPP_SEND_ENABLED", c.send)
			t.Setenv("WHATSAPP_MUTATIONS_ENABLED", c.mutations)
			t.Setenv("WHATSAPP_DO_NOT_CONTACT", c.denied)
			t.Setenv("WHATSAPP_SEND_ALLOWLIST", c.allowed)
			handler := protectBridgeAccess(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(204) }))
			path, method := c.path, http.MethodPost
			if path == "" {
				path, method = "/api/health", http.MethodGet
			}
			r := httptest.NewRequest(method, path, strings.NewReader(c.body))
			r.Header.Set("Authorization", c.authorization)
			w := httptest.NewRecorder()
			handler.ServeHTTP(w, r)
			if w.Code != c.want {
				t.Fatalf("status = %d, want %d", w.Code, c.want)
			}
		})
	}
}
