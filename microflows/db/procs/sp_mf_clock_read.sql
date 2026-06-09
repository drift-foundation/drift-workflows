DELIMITER $$
-- Time SOURCING (read-only) — the single sanctioned clock read in the
-- database layer (§24.4). Transition SPs never read a clock; the runtime
-- sources database_now (and a caller-requested lease deadline derived from
-- it) here, then passes both onward as explicit command parameters, FIXED
-- across retries of the command they parameterize.
CREATE PROCEDURE `sp_mf_clock_read`(
	IN arg_lease_seconds int
)
proc:BEGIN
	DECLARE v_now datetime(6);

	IF arg_lease_seconds IS NULL OR arg_lease_seconds <= 0 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfLeaseSecondsInvalid';
	END IF;

	SET v_now = NOW(6);

	SELECT JSON_OBJECT(
		'outcome', 'ok',
		'now', DATE_FORMAT(v_now, '%Y-%m-%d %H:%i:%s.%f'),
		'lease_expires_at', DATE_FORMAT(DATE_ADD(v_now, INTERVAL arg_lease_seconds SECOND), '%Y-%m-%d %H:%i:%s.%f')
	) AS result;
END $$
DELIMITER ;
