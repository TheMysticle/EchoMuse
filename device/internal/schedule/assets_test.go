package schedule

import (
	"crypto/sha256"
	"encoding/hex"
	"os"
	"path/filepath"
	"testing"
)

func assetID(b []byte) string { h := sha256.Sum256(b); return hex.EncodeToString(h[:]) }
func TestAssetsVerifyAndPruneOnlyUnusedSounds(t *testing.T) {
	dir := t.TempDir()
	one := []byte{1, 0}
	two := []byte{2, 0}
	a, b := assetID(one), assetID(two)
	if err := PutAsset(dir, a, two); err == nil {
		t.Fatal("bad hash accepted")
	}
	if err := PutAsset(dir, a, one); err != nil {
		t.Fatal(err)
	}
	if err := PutAsset(dir, b, two); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "leave-me"), []byte("other file"), 0600); err != nil {
		t.Fatal(err)
	}
	n, err := PruneAssets(dir, []Item{{Sound: a}}, DefaultSettings())
	if err != nil || n != 1 {
		t.Fatal(n, err)
	}
	if _, err = ReadAsset(dir, a); err != nil {
		t.Fatal(err)
	}
	if _, err = ReadAsset(dir, b); err == nil {
		t.Fatal("unused sound retained")
	}
	if _, err = os.Stat(filepath.Join(dir, "leave-me")); err != nil {
		t.Fatal("unrelated file removed")
	}
}
