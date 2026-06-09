#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTANCE_ROOT="${ROOT_DIR}/tmp_db_instances"
COMPOSE_CMD=()

usage() {
	echo "usage:"
	echo "  $0 create <instance> [host_port] [image]"
	echo "  $0 up <instance>"
	echo "  $0 down <instance>"
	echo "  $0 ps <instance>"
	echo "  $0 logs <instance>"
	echo "  $0 rm <instance>"
	echo "  $0 sql <instance> <sql>"
	echo "  $0 schema-load <instance> [sql_file]"
	exit 1
}

require_instance() {
	local instance="$1"
	if [[ -z "${instance}" ]]; then
		echo "error: instance is required" >&2
		usage
	fi
	validate_instance "${instance}"
}

validate_instance() {
	local instance="$1"
	if [[ ! "${instance}" =~ ^mdb[0-9]+-[a-z]$ ]]; then
		echo "error: invalid instance '${instance}' (expected mdb<version>-<slot>, e.g. mdb114-a)" >&2
		exit 1
	fi
}

ensure_under_root() {
	local root="$1"
	local path="$2"
	local root_real path_real
	root_real="$(canonicalize_path "${root}")"
	path_real="$(canonicalize_path "${path}")"
	case "${path_real}" in
		"${root_real}"/*) ;;
		*)
			echo "error: path escapes root: ${path_real} (root: ${root_real})" >&2
			exit 1
			;;
	esac
}

canonicalize_path() {
	local path="$1"
	if command -v realpath >/dev/null 2>&1; then
		if realpath -m / >/dev/null 2>&1; then
			realpath -m "${path}"
			return
		fi
		realpath "${path}"
		return
	fi
	if command -v python3 >/dev/null 2>&1; then
		python3 -c 'import os, sys; print(os.path.abspath(sys.argv[1]))' "${path}"
		return
	fi
	echo "error: cannot canonicalize path '${path}' (need realpath or python3)" >&2
	exit 1
}

read_env_key() {
	local env_file="$1"
	local key="$2"
	local line value
	line="$(grep -E "^${key}=" "${env_file}" | tail -n 1 || true)"
	if [[ -z "${line}" ]]; then
		echo "error: missing ${key} in ${env_file}" >&2
		exit 1
	fi
	value="${line#*=}"
	if [[ "${value}" == *$'\n'* || "${value}" == *$'\r'* ]]; then
		echo "error: invalid ${key} value" >&2
		exit 1
	fi
	printf "%s" "${value}"
}

validate_host_port() {
	local host_port="$1"
	if [[ ! "${host_port}" =~ ^[0-9]+$ ]]; then
		echo "error: invalid host port '${host_port}' (expected numeric)" >&2
		exit 1
	fi
	if ((host_port < 1 || host_port > 65535)); then
		echo "error: host port out of range '${host_port}' (expected 1-65535)" >&2
		exit 1
	fi
}

validate_image() {
	local image="$1"
	if [[ ! "${image}" =~ ^[A-Za-z0-9._/-]+(:[A-Za-z0-9._-]+)?$ ]]; then
		echo "error: invalid image '${image}'" >&2
		exit 1
	fi
}

replace_env_key() {
	local env_file="$1"
	local key="$2"
	local value="$3"
	if sed --version >/dev/null 2>&1; then
		sed -i "s|^${key}=.*|${key}=${value}|" "${env_file}"
		return
	fi
	sed -i '' "s|^${key}=.*|${key}=${value}|" "${env_file}"
}

slot_index() {
	local slot="$1"
	local code
	code=$(printf "%d" "'${slot}")
	echo $((code - 96))
}

derived_port() {
	local instance="$1"
	if [[ "${instance}" =~ ^mdb([0-9]+)-([a-z])$ ]]; then
		local ver="${BASH_REMATCH[1]}"
		local slot="${BASH_REMATCH[2]}"
		local idx
		idx=$(slot_index "${slot}")
		if ((idx < 1)); then
			echo "error: invalid slot in instance '${instance}'" >&2
			exit 1
		fi
		# Predictable port layout:
		# - version bucket by mdb<ver>-*
		# - slot offsets in +5 steps (a=+0, b=+5, c=+10, ...)
		# Example: mdb114-a -> 34114, mdb114-b -> 34119, mdb114-c -> 34124.
		echo $((34000 + ver + (idx - 1) * 5))
		return
	fi
	echo "error: cannot derive host port for '${instance}', expected pattern mdb<version>-<slot> (example: mdb114-a)" >&2
	exit 1
}

render_env() {
	local instance="$1"
	local host_port="$2"
	cat <<EOF
INSTANCE_NAME=${instance}
HOST_PORT=${host_port}
ROOT_PASSWORD=rootpw
APP_DB=appdb
APP_USER=app
APP_PASSWORD=apppw
IMAGE=mariadb:11.4
EOF
}

render_my_cnf() {
	cat <<'EOF'
[mysqld]
character-set-server=utf8mb4
collation-server=utf8mb4_unicode_ci
skip_name_resolve=ON
bind-address=0.0.0.0
EOF
}

render_compose() {
	local runtime_dir="$1"
	local config_dir="$2"
	cat <<EOF
services:
  db:
    image: \${IMAGE}
    container_name: \${INSTANCE_NAME}
    ports:
      - "\${HOST_PORT}:3306"
    environment:
      MARIADB_ROOT_PASSWORD: \${ROOT_PASSWORD}
      MARIADB_DATABASE: \${APP_DB}
      MARIADB_USER: \${APP_USER}
      MARIADB_PASSWORD: \${APP_PASSWORD}
    volumes:
      - ${runtime_dir}/data:/var/lib/mysql
      - ${runtime_dir}/log:/var/log/mysql
      - ${runtime_dir}/tmp:/tmp
      - ${config_dir}/conf.d:/etc/mysql/conf.d
      - ${config_dir}/init:/docker-entrypoint-initdb.d
    command: ["--bind-address=0.0.0.0"]
    restart: unless-stopped
EOF
}

compose_argv_for() {
	local instance="$1"
	local runtime_dir="${INSTANCE_ROOT}/${instance}/runtime"
	local config_dir="${INSTANCE_ROOT}/${instance}/config"
	local env_file="${runtime_dir}/run.env"
	local compose_file="${config_dir}/compose.yaml"
	if [[ ! -f "${env_file}" ]]; then
		echo "error: missing ${env_file}, run create first" >&2
		exit 1
	fi
	if [[ ! -f "${compose_file}" ]]; then
		echo "error: missing ${compose_file}, run create first" >&2
		exit 1
	fi
	if docker compose version >/dev/null 2>&1; then
		COMPOSE_CMD=(docker compose --env-file "${env_file}" -f "${compose_file}")
		return
	fi
	if command -v docker-compose >/dev/null 2>&1; then
		COMPOSE_CMD=(docker-compose --env-file "${env_file}" -f "${compose_file}")
		return
	fi
	echo "error: docker compose is not available" >&2
	exit 1
}

is_container_running() {
	local name="$1"
	local running
	running="$(docker inspect -f '{{.State.Running}}' "${name}" 2>/dev/null || true)"
	[[ "${running}" == "true" ]]
}

ensure_runtime_tmp_permissions() {
	local instance="$1"
	local tmp_dir="${INSTANCE_ROOT}/${instance}/runtime/tmp"
	ensure_under_root "${INSTANCE_ROOT}" "${tmp_dir}"
	mkdir -p "${tmp_dir}"
	chmod 1777 "${tmp_dir}"
}

cmd_create() {
	local instance="$1"
	local host_port="${2:-}"
	local image="${3:-mariadb:11.4}"
	require_instance "${instance}"
	if [[ -z "${host_port}" ]]; then
		host_port="$(derived_port "${instance}")"
	fi
	validate_host_port "${host_port}"
	validate_image "${image}"
	local instance_dir="${INSTANCE_ROOT}/${instance}"
	local runtime_dir="${instance_dir}/runtime"
	local config_dir="${instance_dir}/config"
	ensure_under_root "${INSTANCE_ROOT}" "${instance_dir}"
	ensure_under_root "${INSTANCE_ROOT}" "${runtime_dir}"
	ensure_under_root "${INSTANCE_ROOT}" "${config_dir}"
	if [[ -e "${instance_dir}" ]]; then
		echo "already exists: ${instance_dir}"
		echo "instance '${instance}' already exists; leaving existing files unchanged"
		echo "use '$0 rm ${instance}' to remove and recreate"
		return 0
	fi
	mkdir -p "${runtime_dir}/data" "${runtime_dir}/log" "${runtime_dir}/tmp" "${config_dir}/conf.d" "${config_dir}/init"
	chmod 1777 "${runtime_dir}/tmp"
	touch "${runtime_dir}/.db_instance_marker" "${config_dir}/.db_instance_marker"
	render_env "${instance}" "${host_port}" > "${runtime_dir}/run.env"
	replace_env_key "${runtime_dir}/run.env" "IMAGE" "${image}"
	render_my_cnf > "${config_dir}/conf.d/my.cnf"
	render_compose "${runtime_dir}" "${config_dir}" > "${config_dir}/compose.yaml"
	echo "created instance '${instance}'"
	echo "instance: ${instance_dir}"
	echo "runtime: ${runtime_dir}"
	echo "config:  ${config_dir}"
	echo "port:    ${host_port}"
	echo "image:   ${image}"
}

cmd_up() {
	local instance="$1"
	require_instance "${instance}"
	ensure_runtime_tmp_permissions "${instance}"
	compose_argv_for "${instance}"
	if is_container_running "${instance}"; then
		echo "already running: ${instance}"
		return 0
	fi
	"${COMPOSE_CMD[@]}" up -d
}

cmd_down() {
	local instance="$1"
	require_instance "${instance}"
	compose_argv_for "${instance}"
	if ! is_container_running "${instance}"; then
		echo "already down: ${instance}"
		return 0
	fi
	"${COMPOSE_CMD[@]}" down
}

cmd_ps() {
	local instance="$1"
	require_instance "${instance}"
	compose_argv_for "${instance}"
	"${COMPOSE_CMD[@]}" ps
}

cmd_logs() {
	local instance="$1"
	require_instance "${instance}"
	compose_argv_for "${instance}"
	"${COMPOSE_CMD[@]}" logs -f --tail=200
}

cmd_rm() {
	local instance="$1"
	require_instance "${instance}"
	local instance_dir="${INSTANCE_ROOT}/${instance}"
	local runtime_dir="${instance_dir}/runtime"
	local config_dir="${instance_dir}/config"
	ensure_under_root "${INSTANCE_ROOT}" "${instance_dir}"
	ensure_under_root "${INSTANCE_ROOT}" "${runtime_dir}"
	ensure_under_root "${INSTANCE_ROOT}" "${config_dir}"
	compose_argv_for "${instance}"
	"${COMPOSE_CMD[@]}" down -v --remove-orphans
	if [[ ! -f "${runtime_dir}/.db_instance_marker" || ! -f "${config_dir}/.db_instance_marker" ]]; then
		echo "error: missing instance marker(s), refusing delete for '${instance}'" >&2
		exit 1
	fi
	rm -rf -- "${instance_dir}"
	echo "removed instance '${instance}'"
}

cmd_sql() {
	local instance="$1"
	local sql="$2"
	require_instance "${instance}"
	if [[ -z "${sql}" ]]; then
		echo "error: sql is required" >&2
		usage
	fi
	local runtime_dir="${INSTANCE_ROOT}/${instance}/runtime"
	local env_file="${runtime_dir}/run.env"
	if [[ ! -f "${env_file}" ]]; then
		echo "error: missing ${env_file}, run create first" >&2
		exit 1
	fi
	local instance_name root_password
	instance_name="$(read_env_key "${env_file}" "INSTANCE_NAME")"
	root_password="$(read_env_key "${env_file}" "ROOT_PASSWORD")"
	validate_instance "${instance_name}"
	docker exec -i "${instance_name}" mariadb -uroot "-p${root_password}" -e "${sql}"
}

cmd_schema_load() {
	local instance="$1"
	local sql_file="${2:-${ROOT_DIR}/tests/fixtures/appdb_schema.sql}"
	require_instance "${instance}"
	if [[ ! -f "${sql_file}" ]]; then
		echo "error: missing sql file '${sql_file}'" >&2
		exit 1
	fi
	local runtime_dir="${INSTANCE_ROOT}/${instance}/runtime"
	local env_file="${runtime_dir}/run.env"
	if [[ ! -f "${env_file}" ]]; then
		echo "error: missing ${env_file}, run create first" >&2
		exit 1
	fi
	local host_port root_password
	host_port="$(read_env_key "${env_file}" "HOST_PORT")"
	root_password="$(read_env_key "${env_file}" "ROOT_PASSWORD")"
	validate_host_port "${host_port}"
	mariadb -h 127.0.0.1 -P "${host_port}" -u root "-p${root_password}" < "${sql_file}"
	echo "loaded schema into '${instance}' from ${sql_file}"
}

main() {
	local action="${1:-}"
	case "${action}" in
		create)
			shift
			cmd_create "${1:-}" "${2:-}" "${3:-}"
			;;
		up)
			shift
			cmd_up "${1:-}"
			;;
		down)
			shift
			cmd_down "${1:-}"
			;;
		ps)
			shift
			cmd_ps "${1:-}"
			;;
		logs)
			shift
			cmd_logs "${1:-}"
			;;
		rm)
			shift
			cmd_rm "${1:-}"
			;;
		sql)
			shift
			cmd_sql "${1:-}" "${2:-}"
			;;
		schema-load)
			shift
			cmd_schema_load "${1:-}" "${2:-}"
			;;
		*)
			usage
			;;
	esac
}

main "$@"
