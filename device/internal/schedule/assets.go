package schedule

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
)

// Assets are preconverted 48kHz mono S16 PCM. Only verified content-addressed
// files are playable. Single uploads are bounded; no URL fetch runs on a Dot.
const MaxAssetBytes = 4 * 1024 * 1024

func PutAsset(dir, id string, data []byte) error {
	if !assetPattern.MatchString(id) || len(data) < 2 || len(data)%2 != 0 || len(data) > MaxAssetBytes {
		return errors.New("invalid PCM asset")
	}
	hash := sha256.Sum256(data)
	if hex.EncodeToString(hash[:]) != id {
		return errors.New("asset checksum mismatch")
	}
	if _, err := os.Stat(filepath.Join(dir, id)); err == nil {
		return nil
	}
	files, _ := os.ReadDir(dir)
	var bytes int64
	count := 0
	for _, f := range files {
		if assetPattern.MatchString(f.Name()) {
			info, err := f.Info()
			if err != nil {
				return err
			}
			bytes += info.Size()
			count++
		}
	}
	if count >= 32 || bytes+int64(len(data)) > 64*1024*1024 {
		return errors.New("asset cache full; remove unused assets")
	}
	return atomicFile(filepath.Join(dir, id), data)
}
func ReadAsset(dir, id string) ([]byte, error) {
	if !assetPattern.MatchString(id) {
		return nil, errors.New("invalid asset")
	}
	b, e := os.ReadFile(filepath.Join(dir, id))
	if e != nil {
		return nil, e
	}
	h := sha256.Sum256(b)
	if len(b) > MaxAssetBytes || len(b)%2 != 0 || hex.EncodeToString(h[:]) != id {
		return nil, errors.New("corrupt asset")
	}
	return b, nil
}

// PruneAssets removes only unused content-addressed files. This is explicit,
// not periodic: an upload may legitimately precede its item creation.
func PruneAssets(dir string, items []Item, settings Settings) (int, error) {
	used := map[string]bool{}
	for _, id := range settings.Sounds {
		used[id] = true
	}
	for _, i := range items {
		used[i.Sound] = true
		used[i.Speech] = true
	}
	files, err := os.ReadDir(dir)
	if os.IsNotExist(err) {
		return 0, nil
	}
	if err != nil {
		return 0, err
	}
	removed := 0
	for _, f := range files {
		if assetPattern.MatchString(f.Name()) && !used[f.Name()] {
			if err := os.Remove(filepath.Join(dir, f.Name())); err != nil {
				return removed, err
			}
			removed++
		}
	}
	if removed > 0 {
		d, err := os.Open(dir)
		if err != nil {
			return removed, err
		}
		defer d.Close()
		if err = d.Sync(); err != nil {
			return removed, err
		}
	}
	return removed, nil
}
