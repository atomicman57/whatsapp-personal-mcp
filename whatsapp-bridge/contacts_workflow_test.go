package main

import (
	"go.mau.fi/whatsmeow/appstate"
	"go.mau.fi/whatsmeow/types"
	"testing"
)

func TestContactNamePatchMatchesProtocol(t *testing.T) {
	jid := types.NewJID("15550000001", types.DefaultUserServer)
	patch := contactNamePatch(jid, "Synthetic Person TT")
	if patch.Type != appstate.WAPatchCriticalUnblockLow || len(patch.Mutations) != 1 {
		t.Fatal("incorrect contact patch collection")
	}
	mutation := patch.Mutations[0]
	if mutation.Version != 2 || len(mutation.Index) != 2 || mutation.Index[0] != "contact" || mutation.Index[1] != jid.String() {
		t.Fatal("incorrect contact patch index/version")
	}
	if mutation.Value.GetContactAction().GetFullName() != "Synthetic Person TT" {
		t.Fatal("contact name not preserved")
	}
}
