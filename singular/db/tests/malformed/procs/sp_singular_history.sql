DELIMITER $$
CREATE PROCEDURE `sp_singular_history`(
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
END$$
DELIMITER ;
