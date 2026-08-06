DELIMITER $$
CREATE PROCEDURE `sp_singular_inspect`(
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
	ELSEIF arg_idempotency_key = 0x0F THEN
		-- strict std.json (drift 0.35.0 floor): DUPLICATE TOP-LEVEL KEY in the envelope must be
		-- rejected by the gateway's strict parse. Raw string on purpose: JSON_OBJECT cannot emit
		-- duplicates, and MariaDB's JSON_VALID would ACCEPT this document — the DB cannot backstop
		-- us until the 12.3 migration adds IS JSON OBJECT WITH UNIQUE KEYS guards.
		SELECT '{"outcome":"terminal","outcome":"terminal","state":"done","payload":{"ok":true},"checkpoint":{}}' AS result;
	ELSEIF arg_idempotency_key = 0x10 THEN
		-- strict std.json (drift 0.35.0 floor): duplicate key NESTED inside payload must be rejected.
		SELECT '{"outcome":"terminal","state":"done","payload":{"ok":true,"ok":false},"checkpoint":{}}' AS result;
	ELSE
		-- 0x0E: owner hex decodes to 1 byte, not 16 (checkpoint valid, so the throw is the owner).
		SELECT JSON_OBJECT('outcome','working','lease_owner','00',
			'lease_expires_at','2026-01-01 00:00:00.000000','checkpoint', JSON_EXTRACT('{}','$')) AS result;
	END IF;
END$$
DELIMITER ;
