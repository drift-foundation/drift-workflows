# security-model

## Purpose and status

This note records forward-looking security concepts for Microflows so current
work remains compatible with the intended model. It is architectural guidance,
not an implementation plan. No API, schema, token format, encryption mechanism,
or security product is selected here.

## Core identity

One `workflow_id` identifies one durable workflow instance (also informally
called a workflow session). It is the correlation root for the instance's
revision, arguments, continuation, operation requests/results, checkpoints,
events, and security-context reference.

Subordinate operation, event, and attempt identities remain distinct, but there
is no second workflow-session identifier.

## Security context

Each workflow instance carries a `security_context_ref`.

The reference is:

- an opaque, non-secret identifier, expected to be UUID-like;
- safe to persist in workflow metadata and audit logs;
- useless by itself to an unprivileged observer;
- resolvable into identity and security information only through a privileged
  security authority;
- a reference to actively managed state, not a frozen authorization decision.

Ordinary workflow arguments, results, continuations, checkpoints, and operation
payloads must not carry reusable credentials. They may be durable and broadly
observable to workflow tooling, while credentials can be short-lived, rotated,
revoked, encrypted, and subject to stricter access controls.

Persisting `security_context_ref` in audit records is intentional. It allows a
privileged investigation to determine which security context authorized past
activity without exposing that context through normal workflow data or logs.

## Continuous authorization

Authorization is evaluated when work is about to cross a participant boundary,
including after a deferred workflow resumes. An authorization granted when the
workflow began is not assumed to remain valid.

The security authority may consider more than local identity and role claims.
Its decision can incorporate system-wide and current signals such as:

- request rate, source, and request patterns;
- related workflows or account activity;
- tenant, account, or service state;
- active attack indicators and revocation events;
- the participant and operation being requested;
- the workflow's current state and security context.

This permits access to be withdrawn while a workflow is deferred. Resumption
must obtain a current decision rather than replaying an earlier credential.

## Participant credentials

For an authorized dispatch, the security authority grants a short-lived
credential suitable for the target participant. The participant configuration
and credential provider determine how that grant is represented, for example:

- a bearer token such as a JWT;
- Basic authentication;
- an API-key or custom header;
- a mutually authenticated transport identity;
- another participant-specific proof.

Credentials are transient dispatch material. They should be narrowly scoped to
the intended participant and operation and may also be bound to the workflow,
stable operation identity, request identity, authorization epoch, or attempt.
The exact binding and token format remain open.

Microflows persists only non-secret correlation and audit facts required by the
event model. It does not persist the issued transport credential as an operation
argument, result, or durable continuation value.

## Responsibility boundaries

The workflow DSL and compiled IR declare semantic requirements: the logical
participant, operation, types, schema version, and eventually the required
authorization class or capability.

Trusted deployment configuration resolves logical participants into transport
and credential-provider policy. It may later describe endpoint pools, selection
and failover, TLS, proxies, timeouts, and auth-provider references.

The security authority owns continuous risk evaluation, revocation, and
credential issuance. Microflows supplies trusted context and enforces the
decision at dispatch. Participants validate the presented credential before
performing protected work.

The dispatcher transports a resolved credential but does not implement security
policy or JWT issuance.

## Decisions

- One durable workflow instance has one `workflow_id`; "workflow session" is an
  informal synonym, not another identity.
- A workflow has an opaque `security_context_ref`, recorded in protected durable
  metadata and audit logs.
- The reference is resolved dynamically through a privileged security authority.
- Authorization is checked for each participant dispatch and again after
  deferral/recovery; it is not permanently granted at workflow creation.
- Short-lived participant credentials travel through a separate security
  channel and never become ordinary persisted workflow payloads.
- Security policy may use global behavioral and attack signals, not only a
  caller identity.
- Participant routing/configuration, workflow semantics, and security policy are
  separate concerns.
- Strong revocation has an unavoidable distributed race after credential
  issuance. Sensitive participants must enforce token lifetime, scope, and any
  required revocation/epoch checks when accepting or committing work.

## Intentionally open

- Where the security context is stored and which service resolves it.
- How a gateway creates or associates the context with a new workflow.
- The authorization request/decision protocol and availability behavior.
- Whether denial is terminal, blocked for resolution, or represented by a
  distinct workflow outcome in each policy class.
- Credential formats, encryption, rotation, caching, and refresh mechanisms.
- Exact credential binding to workflow, operation, request hash, or attempt.
- Audit event fields, retention, redaction, and privileged lookup controls.
- Behavior when the security authority is unavailable or returns a challenge.

These choices should be made when the authorization boundary becomes an active
implementation effort, informed by the generic dispatcher and participant
protocol rather than fixed prematurely.

## Current status and next action

**Recorded for alignment; not scheduled for implementation.** The active generic
REST dispatcher effort should preserve this boundary: operation payloads remain
credential-free, participant auth stays behind a resolver/provider abstraction,
and no temporary config shape should make credentials durable workflow data.
