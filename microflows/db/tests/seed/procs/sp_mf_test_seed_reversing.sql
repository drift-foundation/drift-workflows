-- TEST FIXTURE ONLY — NOT a domain transition and NOT part of the production
-- schema. Lives in a SEPARATE Mariachi-managed test schema `microflows_test` (loaded by the test gate via
-- `mariachi apply --schema microflows_test`); it writes into the product `microflows` schema with
-- qualified names. NOT in the product schema; the app/prod role must never be granted EXECUTE here. It seeds a CLAIMABLE reversing(2) workflow with one
-- ACTIVE checkpoint (binding NULL) and a recorded trigger, so the host e2e can
-- claim it (claim_by_id claims state IN (1,2)) and exercise the POSITIVE reversal
-- host decodes — reverse_request Requested, reverse_head Dispatched, reverse_settle
-- Reversed, reverse_block Blocked — which the forward path cannot yet construct
-- (settle completes the single-op workflow; multi-op forward is a later sub-step).
DELIMITER $$
CREATE PROCEDURE `sp_mf_test_seed_reversing`(
	IN arg_workflow_id varbinary(16),
	IN arg_script varchar(190),
	IN arg_cp_op_id varbinary(16),
	IN arg_trigger_op_id varbinary(16),
	IN arg_ts datetime(6)
)
BEGIN
	INSERT INTO `microflows`.`tb_mf_workflow` (
		`workflow_id`, `script_name`, `script_revision`, `state`, `execution_direction`,
		`current_disposition`, `current_event_seq`, `current_event_ts`, `fencing_token`,
		`lease_owner`, `lease_expires_at`, `next_attempt_at`, `current_operation_attempt`,
		`continuation`, `reversal_trigger_operation_id`, `created_at`, `updated_at`
	) VALUES (
		arg_workflow_id, arg_script, 1, 2, 2,
		2, 1, arg_ts, 1,
		NULL, NULL, arg_ts, 0,
		JSON_OBJECT('pos', 'reverse', 'seq', 1), arg_trigger_op_id, arg_ts, arg_ts
	);
	INSERT INTO `microflows`.`tb_mf_workflow_checkpoint` (
		`workflow_id`, `seq`, `operation_name`, `operation_id`, `payload`, `reversal_state`,
		`created_at`, `updated_at`
	) VALUES (
		arg_workflow_id, 1, 'reserve', arg_cp_op_id, JSON_OBJECT('reservation', 'r2'), 1,
		arg_ts, arg_ts
	);
	INSERT INTO `microflows`.`tb_mf_workflow_event` (
		`workflow_id`, `event_seq`, `event_ts`, `kind`, `actor`, `request_id`, `payload`
	) VALUES (
		arg_workflow_id, 1, arg_ts, 'reversal_begun', NULL, NULL, JSON_OBJECT('seeded', 1)
	);
END $$
DELIMITER ;
