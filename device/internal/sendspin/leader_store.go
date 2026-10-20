package sendspin

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"sync"

	"github.com/wilbowes/EchoMuse/internal/atomicfile"
)

// leaderStore is the Echo's identity as a Sendspin SERVER and the keys it
// shares with the players it leads. It is not the player's store: the two
// roles have separate keypairs, so an Echo that is both a Music Assistant
// player and a group leader has two identities and losing one costs nothing
// of the other.
//
// A record is the long-term PSK the controller minted for one (this leader,
// one player) pair and pushed to both ends over the authenticated control
// link. Nothing about the exchange is a secret the controller keeps: if
// either side loses its half, the leader reports needs_key and the controller
// mints a fresh one.
type leaderStore struct {
	path string
	mu   sync.Mutex
	f    leaderFile
	id   identity
}

type leaderFile struct {
	PrivateKey string            `json:"privateKey"`
	Keys       map[string]string `json:"keys,omitempty"` // client_id -> base64url PSK
}

// maxLeaderKeys bounds the file. A group is a handful of Echoes; the cap is
// only there so a controller bug cannot grow it without limit.
const maxLeaderKeys = 64

func openLeaderStore(path string) (*leaderStore, error) {
	s := &leaderStore{path: path}
	b, err := os.ReadFile(path)
	switch {
	case err == nil:
		// A corrupt file is an error rather than a reset: resetting would
		// change the server id and strand every player's record.
		if err := json.Unmarshal(b, &s.f); err != nil {
			return nil, err
		}
	case errors.Is(err, os.ErrNotExist):
	default:
		return nil, err
	}
	dirty := false
	if s.f.PrivateKey == "" {
		id, err := newIdentity()
		if err != nil {
			return nil, err
		}
		s.f.PrivateKey, dirty = b64u(id.priv), true
	}
	priv, err := unb64u(s.f.PrivateKey)
	if err != nil {
		return nil, err
	}
	if s.id, err = identityFrom(priv); err != nil {
		return nil, err
	}
	if dirty {
		if err := s.saveLocked(); err != nil {
			return nil, err
		}
	}
	return s, nil
}

func (s *leaderStore) saveLocked() error {
	b, err := json.MarshalIndent(&s.f, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(s.path), 0o700); err != nil {
		return err
	}
	return atomicfile.Write(s.path, b, 0o600)
}

// serverID is this leader's identity on the wire.
func (s *leaderStore) serverID() string { return s.id.clientID() }

func (s *leaderStore) key(clientID string) []byte {
	s.mu.Lock()
	defer s.mu.Unlock()
	raw, ok := s.f.Keys[clientID]
	if !ok {
		return nil
	}
	p, err := unb64u(raw)
	if err != nil || len(p) != pskSize {
		return nil
	}
	return p
}

func (s *leaderStore) setKey(clientID string, psk []byte) error {
	if _, err := peerKey(clientID); err != nil {
		return err
	}
	if len(psk) != pskSize {
		return errors.New("sendspin: group key is not 32 bytes")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.f.Keys == nil {
		s.f.Keys = map[string]string{}
	}
	if _, have := s.f.Keys[clientID]; !have && len(s.f.Keys) >= maxLeaderKeys {
		return errors.New("sendspin: too many group keys")
	}
	s.f.Keys[clientID] = b64u(psk)
	return s.saveLocked()
}

func (s *leaderStore) dropKey(clientID string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.f.Keys[clientID]; !ok {
		return
	}
	delete(s.f.Keys, clientID)
	s.saveLocked()
}
