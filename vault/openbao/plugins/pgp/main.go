// Seine PGP plugin for OpenBao: apt-repository signing as a Vault
// API ("send the object, get the signed object back"). The private
// key never leaves the server; clients only ever see bytes over HTTP.
package main

import (
	"log"

	"github.com/openbao/openbao/sdk/v2/plugin"
)

func main() {
	if err := plugin.Serve(&plugin.ServeOpts{
		BackendFactoryFunc: Factory,
	}); err != nil {
		log.Fatal(err)
	}
}
