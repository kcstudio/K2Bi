# Spec B Section 6 Round 2 Kimi Disposition

Review log: `.code-reviews/2026-05-11T07-13-04Z_59708a.log`

Kimi verdict: NEEDS-ATTENTION.

Codex disposition: one allocator finding accepted and fixed at `334d318`; one F6 documentation clarification accepted at `334d318`; two requested hardening directions rejected as outside the Section 6 named-bug surface.

## Finding 1

Status: ACCEPTED - fixed at `334d318`

Code anchor: `scripts/lib/clientid_allocator.py:77` now treats any lease with a positive but non-live `owner_pid` as stale immediately: `return not _pid_is_alive(owner_pid)`.

Code anchor: `scripts/lib/clientid_allocator.py:117` reclaims stale lease files under the allocator flock before issuing the same preferred clientId to a new caller.

Test anchor: `tests/test_engine_gateway_discipline.py:76` covers a fresh dead owner inside the TTL. `tests/test_engine_gateway_discipline.py:50` still covers an older dead owner.

Safety reasoning: Kimi was correct that a dead-owner lease should not wait for the TTL. The TTL now only handles legacy or malformed lease records without a usable positive owner PID. Normal `gateway-query.sh` leases use the shell PID as owner, so a killed shell makes the lease immediately reclaimable.

## Finding 2

Status: ACCEPTED - documented at `334d318`; REJECTED for authentication hardening

Code anchor: `scripts/gateway-query.sh:41` now states the F6 threat model directly: `This guard is an accidental-misuse safety rail, not an authentication boundary.`

Code anchor: `scripts/gateway-query.sh:42` documents that `K2BI_GATEWAY_QUERY_OPERATOR_OVERRIDE` is intentionally available to the operator. The load-bearing accidental-skill block remains `CLAUDE_CODE_SKILL_INVOCATION` at `scripts/gateway-query.sh:48`.

Test anchor: `tests/test_engine_gateway_discipline.py:102` requires the script to retain both the skill sentinel and the "not an authentication boundary" wording.

Safety reasoning: The documentation part of Kimi's finding was valid and is fixed. The requested capability/nonce/authentication redesign is outside F6. F6 is a caller-context safety rail for accidental skill misuse, not a local authentication mechanism against a malicious operator, compromised local account, or modified repo. The override must remain available because the operator owns the live broker diagnostic path. Strengthening this into a cryptographic or systemd attestation boundary would require new spec text and a broader threat model.

## Finding 3

Status: REJECTED

Code anchor: `scripts/gateway-query.sh:97` extracts an explicit `clientId=<n>` from the snippet and `scripts/gateway-query.sh:109` allocates the preferred or generated clientId locally before any SSH execution.

Code anchor: `scripts/gateway-query.sh:123` refuses to continue if allocator output is missing, and `scripts/gateway-query.sh:138` overwrites the remote `K2BI_GATEWAY_CLIENT_ID` environment with the locally allocated clientId.

Test anchor: `tests/test_engine_gateway_discipline.py:15` and `tests/test_engine_gateway_discipline.py:76` cover local duplicate-preferred leases, lease release, and the script contract that every invocation goes through the allocator before remote execution.

Safety reasoning: Kimi's remote-token validation request is outside the Section 6 local allocator contract. The implemented single source of truth is the MacBook-local `gateway-query.sh` lease directory, and F6 limits this operator helper to the MacBook unless the operator explicitly overrides. Same-host concurrent sessions share the same flocked lease directory. A remote broker-side token validator would require a new remote entry point or daemon and a shared secret protocol, neither of which is in Spec B Section 6. That is a separate distributed-lease design, not a fix for the named F1/F6 bugs.
