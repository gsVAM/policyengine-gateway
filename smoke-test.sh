#!/bin/bash
# Usage: ./smoke-test.sh <base-url> <api-key>
# Checks: health is open, missing/wrong key -> 401, right key -> a real calculation.
set -u
URL="${1:?base url}"; KEY="${2:?api key}"; fail=0
check() { [ "$2" = "$3" ] && echo "PASS $1" || { echo "FAIL $1 (got $2, want $3)"; fail=1; }; }

check "healthcheck open" "$(curl -s -o /dev/null -w '%{http_code}' "$URL/healthcheck")" 200
check "no key -> 401"    "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$URL/calculate")" 401
check "bad key -> 401"   "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Authorization: Bearer nope' "$URL/calculate")" 401

body='{"households":[{"id":"smoke","tax_year":2026,"filing_status":"SINGLE","situation":{
"people":{"you":{"age":{"2026":40},"employment_income":{"2026":50000}}},
"tax_units":{"tu":{"members":["you"],"income_tax":{"2026":null}}},
"families":{"f":{"members":["you"]}},"spm_units":{"s":{"members":["you"]}},
"marital_units":{"m":{"members":["you"]}},
"households":{"h":{"members":["you"],"state_name":{"2026":"CA"}}}}}]}'
resp="$(curl -s -m 60 -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d "$body" "$URL/calculate")"
out="$(printf '%s' "$resp" | python3 -c 'import sys,json; d=json.load(sys.stdin); r=d["results"][0]
print("income_tax 2026 =", r["situation"]["tax_units"]["tu"]["income_tax"]["2026"], "| brackets", len(r["ordinary_brackets"]), "| identity", d["identity"])' 2>/dev/null)"
if [ -n "$out" ]; then echo "PASS calculate ($out)"; else echo "FAIL calculate: ${resp:0:300}"; fail=1; fi
exit $fail
