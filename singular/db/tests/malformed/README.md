# `singular_malformed` test fixture (Mariachi template)

Loaded by the singular test gate via `mariachi apply --schema singular_malformed`. A separate,
tables-less schema whose two procs deliberately return contract-violating documents to drive the
gateway's decode/error path (see malformed_backend_test.drift, which points its connection at
`singular_malformed`). NOT a product schema; the app/prod role must never be granted EXECUTE here.

## Original rationale

> -- Isolated malformed-backend fixture — decode-side JSON object-contract regression (part of the
> -- e2e gate). NOT the product schema: a SEPARATE schema, `singular_malformed`, whose inspect + history
> -- SPs return hand-built `result` documents per idempotency key so the gateway's decode path can be
> -- driven across the full object contract.
> --
> -- The gateway requires every SP result to be ONE row, ONE column `result`, holding a JSON DOCUMENT
> -- that is a non-null JSON OBJECT, discriminated by `outcome`; terminal payload + checkpoint are
> -- required NESTED JSON objects (re-encoded compact for delivery); lease_owner is lowercase hex of
> -- exactly 16 bytes. This fixture exercises ACCEPT (valid documents) and REJECT (SQL NULL / malformed
> -- JSON / JSON null / array / scalar, at the envelope AND the nested-payload level, plus bad owner hex
> -- / non-object checkpoint).
> --
> -- The control (accept) keys matter: they prove the SP exists, the signature matches, the CALL
> -- succeeds, and the gateway parses a well-formed document WITHOUT throwing — so a reject key's throw
> -- can ONLY be the object-contract check firing, never a missing proc / signature / backend rejection
> -- (which would make the regression pass vacuously). We can't assert the exact exception kind
> -- (typed-catch can't project the non-scalar `kind` this toolchain); the SP-invariant raw-SQL track
> -- pins the error_code.
> --
> -- Keys (1-byte; the gateway requires 1..32-byte keys):
> --   ACCEPT
> --     0x01 terminal Done, valid object payload                 -> Terminal(Done)
> --     0x0A working, valid owner hex + {} checkpoint            -> Working
> --     0x0B terminal Done, payload object with a NESTED array   -> Terminal(Done)  (nested arrays are fine)
> --   REJECT — nested terminal payload is not a JSON object
> --     0x02 payload is a JSON string scalar                     -> doc-field-not-object
> --     0x03 payload is JSON null                                -> doc-field-not-object
> --     0x04 payload is a JSON array                             -> doc-field-not-object
> --     0x05 payload is a JSON number scalar                     -> doc-field-not-object
> --   REJECT — the result envelope is not a JSON object
> --     0x06 envelope is a JSON array                            -> result-not-object
> --     0x07 envelope is SQL NULL                                -> row-required-null
> --     0x08 envelope is malformed JSON                          -> result-not-json
> --     0x09 envelope is a JSON scalar                           -> result-not-object
> --   REJECT — working snapshot fields
> --     0x0C checkpoint is a non-object                          -> doc-field-not-object
> --     0x0D lease_owner is not valid hex                        -> inspect-owner-hex
> --     0x0E lease_owner hex decodes to != 16 bytes              -> inspect-owner-hex
> --
> -- Loaded into `singular_malformed` by the root `just db-load-schema`; exercised by
> -- tests/fixtures/malformed_backend_test.drift, which is part of the e2e gate.
