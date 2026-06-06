#!/usr/bin/env bash

pearl_trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

pearl_load_env_file() {
  local file="$1"
  local raw line key value quote last
  while IFS= read -r raw || [[ -n "$raw" ]]; do
    raw="${raw%$'\r'}"
    line="$(pearl_trim "$raw")"
    [[ -z "$line" || "${line:0:1}" == "#" ]] && continue
    if [[ "$line" == export[[:space:]]* ]]; then
      line="$(pearl_trim "${line#export}")"
    fi
    if [[ "$line" != *"="* ]]; then
      echo "Invalid config line in $file: $raw" >&2
      return 1
    fi
    key="$(pearl_trim "${line%%=*}")"
    value="$(pearl_trim "${line#*=}")"
    if [[ ! "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
      echo "Invalid config key in $file: $key" >&2
      return 1
    fi
    if (( ${#value} >= 2 )); then
      quote="${value:0:1}"
      last="${value: -1}"
      if [[ "$quote" == "$last" && ( "$quote" == "'" || "$quote" == '"' ) ]]; then
        value="${value:1:${#value}-2}"
        if [[ "$quote" == '"' ]]; then
          value="${value//\\\$/\$}"
          value="${value//\\\`/\`}"
          value="${value//\\\"/\"}"
          value="${value//\\\\/\\}"
        fi
      fi
    fi
    printf -v "$key" '%s' "$value"
    export "$key"
  done < "$file"
}
