-- Isolated malformed-backend fixture — decode-side JSON object-contract regression (part of the
-- e2e gate). NOT the product schema: a SEPARATE schema, `singular_malformed`, whose inspect + history
-- SPs return hand-built `result` documents per idempotency key so the gateway's decode path can be
-- driven across the full object contract.
--
-- The gateway requires every SP result to be ONE row, ONE column `result`, holding a JSON DOCUMENT
-- that is a non-null JSON OBJECT, discriminated by `outcome`; terminal payload + checkpoint are
-- required NESTED JSON objects (re-encoded compact for delivery); lease_owner is lowercase hex of
-- exactly 16 bytes. This fixture exercises ACCEPT (valid documents) and REJECT (SQL NULL / malformed
-- JSON / JSON null / array / scalar, at the envelope AND the nested-payload level, plus bad owner hex
-- / non-object checkpoint).
--
-- The control (accept) keys matter: they prove the SP exists, the signature matches, the CALL
-- succeeds, and the gateway parses a well-formed document WITHOUT throwing — so a reject key's throw
-- can ONLY be the object-contract check firing, never a missing proc / signature / backend rejection
-- (which would make the regression pass vacuously). We can't assert the exact exception kind
-- (typed-catch can't project the non-scalar `kind` this toolchain); the SP-invariant raw-SQL track
-- pins the error_code.
--
-- Keys (1-byte; the gateway requires 1..32-byte keys):
--   ACCEPT
--     0x01 terminal Done, valid object payload                 -> Terminal(Done)
--     0x0A working, valid owner hex + {} checkpoint            -> Working
--     0x0B terminal Done, payload object with a NESTED array   -> Terminal(Done)  (nested arrays are fine)
--   REJECT — nested terminal payload is not a JSON object
--     0x02 payload is a JSON string scalar                     -> doc-field-not-object
--     0x03 payload is JSON null                                -> doc-field-not-object
--     0x04 payload is a JSON array                             -> doc-field-not-object
--     0x05 payload is a JSON number scalar                     -> doc-field-not-object
--   REJECT — the result envelope is not a JSON object
--     0x06 envelope is a JSON array                            -> result-not-object
--     0x07 envelope is SQL NULL                                -> row-required-null
--     0x08 envelope is malformed JSON                          -> result-not-json
--     0x09 envelope is a JSON scalar                           -> result-not-object
--   REJECT — working snapshot fields
--     0x0C checkpoint is a non-object                          -> doc-field-not-object
--     0x0D lease_owner is not valid hex                        -> inspect-owner-hex
--     0x0E lease_owner hex decodes to != 16 bytes              -> inspect-owner-hex
--
-- Loaded into `singular_malformed` by the root `just db-load-schema`; exercised by
-- tests/fixtures/malformed_backend_test.drift, which is part of the e2e gate.

DROP DATABASE IF EXISTS `singular_malformed`;
CREATE DATABASE `singular_malformed`;

DELIMITER $$

-- Every actionable SP returns one `result` JSON document; inspect drives the shared decode path
-- (_read_result_doc + _terminal_from_doc + _doc_object_text_req + _hex16) and history drives the
-- per-row decode + (event, status) cross-check (_parse_object_doc + _work_event_from_name +
-- _check_event_status).
CREATE PROCEDURE `singular_malformed`.`sp_singular_inspect`(
	IN arg_service_group varchar(64),
	IN arg_idempotency_key varbinary(32)
)
BEGIN
	DECLARE v_owner16 char(32) DEFAULT LOWER(HEX(UNHEX('000102030405060708090A0B0C0D0E0F')));  -- 16 bytes
	IF arg_idempotency_key = 0x01 THEN
		SELECT JSON_OBJECT('outcome','terminal','state','done',
			'payload', JSON_EXTRACT('{"ok":true}','$'), 'checkpoint', JSON_EXTRACT('{}','$')) AS result;
	ELSEIF arg_idempotency_key = 0x0A THEN
		SELECT JSON_OBJECT('outcome','working','lease_owner', v_owner16,
			'lease_expires_at','2026-01-01 00:00:00.000000','checkpoint', JSON_EXTRACT('{}','$')) AS result;
	ELSEIF arg_idempotency_key = 0x0B THEN
		SELECT JSON_OBJECT('outcome','terminal','state','done',
			'payload', JSON_EXTRACT('{"items":[1,2]}','$'), 'checkpoint', JSON_EXTRACT('{}','$')) AS result;
	ELSEIF arg_idempotency_key = 0x02 THEN
		SELECT JSON_OBJECT('outcome','terminal','state','done','payload','this is not json','checkpoint', JSON_EXTRACT('{}','$')) AS result;
	ELSEIF arg_idempotency_key = 0x03 THEN
		SELECT JSON_OBJECT('outcome','terminal','state','done','payload', JSON_EXTRACT('null','$'),'checkpoint', JSON_EXTRACT('{}','$')) AS result;
	ELSEIF arg_idempotency_key = 0x04 THEN
		SELECT JSON_OBJECT('outcome','terminal','state','done','payload', JSON_EXTRACT('[1,2]','$'),'checkpoint', JSON_EXTRACT('{}','$')) AS result;
	ELSEIF arg_idempotency_key = 0x05 THEN
		SELECT JSON_OBJECT('outcome','terminal','state','done','payload', JSON_EXTRACT('5','$'),'checkpoint', JSON_EXTRACT('{}','$')) AS result;
	ELSEIF arg_idempotency_key = 0x06 THEN
		SELECT JSON_ARRAY('outcome','terminal') AS result;                       -- envelope is an array
	ELSEIF arg_idempotency_key = 0x07 THEN
		SELECT NULL AS result;                                                   -- envelope is SQL NULL
	ELSEIF arg_idempotency_key = 0x08 THEN
		SELECT '{not valid json' AS result;                                      -- envelope is malformed JSON
	ELSEIF arg_idempotency_key = 0x09 THEN
		SELECT '5' AS result;                                                    -- envelope is a JSON scalar
	ELSEIF arg_idempotency_key = 0x0C THEN
		SELECT JSON_OBJECT('outcome','working','lease_owner', v_owner16,
			'lease_expires_at','2026-01-01 00:00:00.000000','checkpoint', JSON_EXTRACT('[1,2]','$')) AS result;   -- checkpoint not an object
	ELSEIF arg_idempotency_key = 0x0D THEN
		SELECT JSON_OBJECT('outcome','working','lease_owner','zznot-hexzz',
			'lease_expires_at','2026-01-01 00:00:00.000000','checkpoint', JSON_EXTRACT('{}','$')) AS result;  -- owner not hex (checkpoint valid)
	ELSE
		-- 0x0E: owner hex decodes to 1 byte, not 16 (checkpoint valid, so the throw is the owner).
		SELECT JSON_OBJECT('outcome','working','lease_owner','00',
			'lease_expires_at','2026-01-01 00:00:00.000000','checkpoint', JSON_EXTRACT('{}','$')) AS result;
	END IF;
END $$

-- history: one event document per row, keyed by `event`; rows carry `status` for the gateway's
-- (event, status) cross-check. 0x01 = matched CONTROL (accept), 0x02 = COMPLETED+WORKING mismatch
-- (the gateway's decode cross-check must reject — the product schema's CHECK makes it unrepresentable
-- there, but a non-conforming backend could still emit it).
CREATE PROCEDURE `singular_malformed`.`sp_singular_history`(
	IN arg_service_group varchar(64),
	IN arg_idempotency_key varbinary(32)
)
BEGIN
	DECLARE v_owner16 char(32) DEFAULT LOWER(HEX(UNHEX('000102030405060708090A0B0C0D0E0F')));  -- 16 bytes
	IF arg_idempotency_key = 0x01 THEN
		SELECT JSON_OBJECT('event','claimed','status',1,'event_ts','2026-01-01 00:00:00.000000',
			'item_meta', JSON_EXTRACT('{}','$'), 'lease_owner', v_owner16, 'lease_meta', JSON_EXTRACT('{}','$'),
			'lease_expires_at','2026-01-01 00:00:30.000000', 'event_payload', JSON_EXTRACT('{}','$'),
			'checkpoint', JSON_EXTRACT('{}','$')) AS result;
	ELSE
		SELECT JSON_OBJECT('event','completed','status',1,'event_ts','2026-01-01 00:00:00.000000',
			'item_meta', JSON_EXTRACT('{}','$'), 'lease_owner', v_owner16, 'lease_meta', JSON_EXTRACT('{}','$'),
			'event_payload', JSON_EXTRACT('{}','$'), 'checkpoint', JSON_EXTRACT('{}','$')) AS result;
	END IF;
END $$

DELIMITER ;
