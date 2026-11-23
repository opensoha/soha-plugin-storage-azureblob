package main

import (
	"context"
	"crypto/rand"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/opensoha/soha/contracts/plugins/storagedriver/sdk"
)

// The release build supplies both values from the root manifest.
var pluginID, version string

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	var driver azureDriver
	rand.Read(driver.cursorKey[:])
	opts := sdk.ServerOptions{PluginID: pluginID, Version: version}
	err := sdk.Serve(ctx, os.Stdin, opts, driver)
	stop()
	if err != nil {
		slog.Error("storage driver stopped before completing service")
		os.Exit(1)
	}
}
