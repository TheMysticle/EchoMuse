package config

import (
	"encoding/json"
	"testing"
)

func role(s string) *string { return &s }

func TestAnEchoIsInNoGroupUntilTold(t *testing.T) {
	d := &Device{}
	d.Apply(ConfigMessage{})
	s := d.Snapshot()
	if s.GroupRole == nil || *s.GroupRole != "" || s.GroupLead != nil {
		t.Fatalf("a fresh device reports role %v lead %v", s.GroupRole, s.GroupLead)
	}
}

func TestGroupLeadIsKeptForALeaderOnly(t *testing.T) {
	lead := &GroupLead{Name: "Kitchen", Channel: "left", Members: []GroupMember{
		{ID: "a", ClientID: "c1", Host: "10.0.0.2", Channel: "right"}}}
	d := &Device{}

	// A lead sent to an Echo that is not a leader is not kept.
	d.Apply(ConfigMessage{GroupLead: lead})
	if d.GroupLead != nil {
		t.Error("a lead was kept by an Echo that is not a leader")
	}
	d.Apply(ConfigMessage{GroupRole: role("member"), GroupLead: lead})
	if d.GroupRole != "member" || d.GroupLead != nil {
		t.Errorf("member holds role %q lead %v", d.GroupRole, d.GroupLead)
	}

	d.Apply(ConfigMessage{GroupRole: role("leader"), GroupLead: lead})
	if d.GroupRole != "leader" || d.GroupLead == nil || len(d.GroupLead.Members) != 1 {
		t.Fatalf("leader holds role %q lead %v", d.GroupRole, d.GroupLead)
	}

	// Leaving the group (an empty role is a value, not an absence) clears it.
	d.Apply(ConfigMessage{GroupRole: role("")})
	if d.GroupRole != "" || d.GroupLead != nil {
		t.Errorf("after leaving: role %q lead %v", d.GroupRole, d.GroupLead)
	}
}

func TestAConfigPushWithoutGroupFieldsLeavesTheGroupAlone(t *testing.T) {
	d := &Device{}
	d.Apply(ConfigMessage{GroupRole: role("leader"), GroupLead: &GroupLead{Name: "G"}})
	d.Apply(ConfigMessage{SendspinName: "Echo"})
	if d.GroupRole != "leader" || d.GroupLead == nil {
		t.Error("an unrelated push dissolved the group")
	}
}

func TestUnknownRoleIsNoRole(t *testing.T) {
	d := &Device{}
	d.Apply(ConfigMessage{GroupRole: role("conductor"), GroupLead: &GroupLead{Name: "G"}})
	if d.GroupRole != "" || d.GroupLead != nil {
		t.Errorf("role %q lead %v", d.GroupRole, d.GroupLead)
	}
}

// The snapshot must not point into the live struct: the lead is read after
// the lock is released, while the next push rewrites it.
func TestSnapshotLeadIsACopy(t *testing.T) {
	d := &Device{}
	d.Apply(ConfigMessage{GroupRole: role("leader"), GroupLead: &GroupLead{Name: "G", Members: []GroupMember{{ID: "a"}}}})
	s := d.Snapshot()
	s.GroupLead.Members[0].ID = "changed"
	s.GroupLead.Name = "changed"
	if d.GroupLead.Members[0].ID != "a" || d.GroupLead.Name != "G" {
		t.Error("the snapshot shares memory with the live config")
	}
	in := &GroupLead{Members: []GroupMember{{ID: "x"}}}
	d.Apply(ConfigMessage{GroupLead: in})
	in.Members[0].ID = "y"
	if d.GroupLead.Members[0].ID != "x" {
		t.Error("the applied lead shares memory with the message")
	}
}

func TestGroupFieldsUseTheNamesTheControllerSends(t *testing.T) {
	raw := `{"groupRole":"leader","groupLead":{"name":"G","channel":"left","members":[` +
		`{"id":"d1","clientId":"abc","host":"10.0.0.5","port":8928,"channel":"right"}]}}`
	var m ConfigMessage
	if err := json.Unmarshal([]byte(raw), &m); err != nil {
		t.Fatal(err)
	}
	if m.GroupRole == nil || *m.GroupRole != "leader" || m.GroupLead == nil {
		t.Fatalf("decoded %+v", m)
	}
	g := m.GroupLead.Members[0]
	if g.ID != "d1" || g.ClientID != "abc" || g.Host != "10.0.0.5" || g.Port != 8928 || g.Channel != "right" {
		t.Errorf("member decoded as %+v", g)
	}
	// And an empty role survives the round trip, since it means "leave".
	out, _ := json.Marshal(ConfigMessage{GroupRole: role("")})
	if string(out) != `{"groupRole":""}` {
		t.Errorf("empty role marshals as %s", out)
	}
}
