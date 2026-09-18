# =============================================================================
# Vault server configuration (production, file backend).
#
# Enabled only through `--profile vault`; dependency-lock.json still records
# `source_locked_runtime_poc_and_legal_review_blocked` for hashicorp-vault
# (BUSL-1.1 legal review outstanding), so no runtime path may treat
# Vault-backed secrets as available yet.
#
# The listener is bound to the container interface on the internal `vaultpriv`
# network only: no host port is published and that network has no external route.
# TLS is terminated by the consumer contract (VAULT_CACERT) once the
# workload-identity PoC passes; until then the listener stays plaintext, because
# a self-signed bootstrap certificate would only fake verification.
#
# Only long-stable directives are used here: an unknown key makes Vault exit,
# and a crash-looping secret store is worse than a missing one.
# =============================================================================

ui            = false
disable_mlock = true

log_level  = "warn"
log_format = "json"
log_file   = "/vault/logs/vault.json"

api_addr     = "http://0.0.0.0:8200"
cluster_addr = "http://0.0.0.0:8201"

listener "tcp" {
  address         = "0.0.0.0:8200"
  cluster_address = "0.0.0.0:8201"
  tls_disable     = 1
}

# File backend: single-node Vault with an encrypted on-disk barrier. The data
# volume is reachable only by this container, and unseal material is never
# stored on it — it is supplied out of band by the operator.
storage "file" {
  path = "/vault/file"
}

# An audit device is enabled at bootstrap by
# deploy/scripts/bootstrap-vault-workload-identity.sh. Vault refuses further
# requests when no audit device is enabled, so an unaudited store cannot serve
# WorkBuddy secrets.
