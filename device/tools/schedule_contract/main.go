// schedule_contract runs a single real scheduler command against an on-disk
// state file. The MCP integration harness invokes it again to verify reboot
// recovery without pretending its simulated transport is physical hardware.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"time"

	"github.com/wilbowes/EchoMuse/internal/schedule"
)

func main() {
	if len(os.Args) != 3 {
		panic("usage: schedule_contract STATE_PATH OWNER")
	}
	engine, err := schedule.Open(os.Args[1], os.Args[2], time.Now, func() bool { return true })
	if err != nil {
		panic(err)
	}
	var command schedule.Command
	if err = json.NewDecoder(os.Stdin).Decode(&command); err != nil {
		panic(err)
	}
	snapshot, err := engine.Apply(command)
	reply := map[string]any{"snapshot": snapshot}
	if err != nil {
		reply["error"] = err.Error()
	}
	if err = json.NewEncoder(os.Stdout).Encode(reply); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
