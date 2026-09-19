# =============================================================================
# Vault policy for the WorkBuddy application workload identity (AppRole).
#
# Applied by the operator once the `vault` profile is provisioned: this
# deployment ships no bootstrap script for it (the profile stays a placeholder
# while dependency-lock.json marks Vault legal/runtime review as blocked, so no
# runtime path may assume Vault-backed secrets exist). Default-deny:
# the workload may read exactly the secret paths it owns and nothing else, and
# may not manage auth methods, policies, tokens, or the audit log.
#
# Path placeholders are rendered at bootstrap time:
#   __VAULT_KV_MOUNT__  KV v2 mount name (default: workbuddy)
#   __VAULT_TENANT__    tenant slug this deployment serves
# =============================================================================

# Read-only access to this tenant's secret subtree (KV v2: data + metadata).
path "__VAULT_KV_MOUNT__/data/tenants/__VAULT_TENANT__/*" {
  capabilities = ["read"]
}

path "__VAULT_KV_MOUNT__/metadata/tenants/__VAULT_TENANT__/*" {
  capabilities = ["read", "list"]
}

# Renewal of its own token only: no token creation, no auth-method changes.
path "auth/token/renew-self" {
  capabilities = ["update"]
}

path "auth/token/lookup-self" {
  capabilities = ["read"]
}

# Explicitly absent: sys/*, auth/* (except renew-self/lookup-self), policy
# writes, and any cross-tenant path. A denied path yields 403, never a fallback.
