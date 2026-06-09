DELIMITER $$
-- Raw audit trail. Exempt from actionable-state narrowing (no `outcome`), but NOT from the DB
-- document transport contract: each event is ONE JSON OBJECT (column `result`) whose `event` field
-- is the natural discriminator (claimed/extended/completed/failed). meta/payload/checkpoint are
-- NESTED objects; lease_owner is lowercase hex (16 bytes); `lease_expires_at` is OMITTED for
-- non-lease/terminal events (never SQL/JSON null). `status` IS transported (alongside the schema
-- CHECK) so the gateway can cross-check the event/status pair on decode; it is not re-exposed.
-- Oldest-first.
CREATE PROCEDURE `sp_singular_history`(
	IN arg_service_group varchar(64),
	IN arg_idempotency_key varbinary(32)
)
BEGIN
	SELECT JSON_REMOVE(
		JSON_OBJECT(
			'event', CASE h.`event_type`
				WHEN 10 THEN 'claimed' WHEN 11 THEN 'extended'
				WHEN 20 THEN 'completed' WHEN 40 THEN 'failed' END,
			'status', h.`status`,
			'event_ts', h.`event_ts`,
			'item_meta', JSON_EXTRACT(h.`item_meta`, '$'),
			'lease_owner', LOWER(HEX(h.`lease_owner`)),
			'lease_meta', JSON_EXTRACT(h.`lease_meta`, '$'),
			'lease_expires_at', h.`lease_expires_at`,
			'event_payload', JSON_EXTRACT(h.`event_payload`, '$'),
			'checkpoint', JSON_EXTRACT(h.`checkpoint_payload`, '$')
		),
		-- omit lease_expires_at when absent (non-lease/terminal events); no-op path otherwise
		IF(h.`lease_expires_at` IS NULL, '$.lease_expires_at', '$.__present__')
	) AS result
	FROM `tb_singular_work_item_history` h
	WHERE h.`service_group` = arg_service_group
		AND h.`idempotency_key` = arg_idempotency_key
	ORDER BY h.`event_ts` ASC;
END $$
DELIMITER ;
