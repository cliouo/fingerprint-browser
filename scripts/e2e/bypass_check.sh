#!/usr/bin/env bash
# Proxy bypass for an extension's backend, with a real browser (CI: Ubuntu + Xvfb).
# Expects the mock backend on 0.0.0.0:8100, DISPLAY and FPB_HOME set.
#
# runner_ext is rewritten to call the mock at this machine's LAN address (a
# browser proxies that, unlike 127.0.0.1) and to open https://example.com/ as
# its runner tab. Through a local HTTP proxy with a password:
#   off: no bypass -> the backend calls go to the proxy (407: a background
#        request has no page to answer the proxy's password challenge)
#   on:  fpb set-bypass <LAN IP> -> 3 results reach the mock, none of the
#        backend calls touch the proxy, the runner tab and the geoip lookup do.
set -euo pipefail
OUT="${RUNNER_TEMP:-/tmp}"
IP=$(hostname -I | awk '{print $1}')
EXT="$OUT/runner_ext_lan"
rm -rf "$EXT" && cp -r scripts/e2e/runner_ext "$EXT"
sed -i "s#^const MOCK = .*#const MOCK = \"http://$IP:8100\";#; s#^const RUNNER_URL = .*#const RUNNER_URL = \"https://example.com/\";#" "$EXT/background.js"
python3 - "$EXT/manifest.json" "$IP" <<'PY'
import json, sys
p, ip = sys.argv[1], sys.argv[2]
m = json.load(open(p)); m["host_permissions"].append(f"http://{ip}/*"); json.dump(m, open(p, "w"), indent=2)
PY
PASS=$(openssl rand -hex 12)
PROXY_PASS="$PASS" uv run python scripts/e2e/local_proxy.py http --port 18080 --user ciuser > "$OUT/bypass-proxy.log" 2>&1 &
sleep 2
uv run fpb new bp --proxy "http://ciuser:$PASS@127.0.0.1:18080" --extension "$EXT" | tee "$OUT/bypass-new.txt"
uv run fpb check bp | tee "$OUT/bypass-check-off.txt"
results() { curl -s http://127.0.0.1:8100/__state | python3 -c "import json,sys;print(len(json.load(sys.stdin)['results']))"; }

echo "--- bypass off"
r0=$(results); m0=$(wc -l < "$OUT/bypass-proxy.log")
uv run python scripts/e2e/window_lifetime.py bp --watch 75 | tee "$OUT/bypass-off.txt" || true
r1=$(results); off=$(tail -n +$((m0 + 1)) "$OUT/bypass-proxy.log")
off_backend=$(grep -c "$IP" <<< "$off" || true); off_407=$(grep -c "^407.*$IP" <<< "$off" || true)

echo "--- bypass on"
uv run fpb set-bypass bp "$IP"
uv run fpb check bp | tee "$OUT/bypass-check-on.txt"
m1=$(wc -l < "$OUT/bypass-proxy.log")
uv run python scripts/e2e/window_lifetime.py bp --watch 75 | tee "$OUT/bypass-on.txt"
r2=$(results); on=$(tail -n +$((m1 + 1)) "$OUT/bypass-proxy.log")
on_backend=$(grep -c "$IP" <<< "$on" || true); on_site=$(grep -ci "example.com" <<< "$on" || true)

grep -E "^(exit IP|timezone|webrtc ip):" "$OUT/bypass-check-off.txt" > "$OUT/geo-off.txt"
grep -E "^(exit IP|timezone|webrtc ip):" "$OUT/bypass-check-on.txt" > "$OUT/geo-on.txt"
echo "off: results +$((r1 - r0)), backend requests at the proxy $off_backend (407: $off_407)"
echo "on:  results +$((r2 - r1)), backend requests at the proxy $on_backend, runner-tab requests at the proxy $on_site"
echo "geoip identical with and without bypass: $(cmp -s "$OUT/geo-off.txt" "$OUT/geo-on.txt" && echo yes || echo NO)"
grep -q "$PASS" "$OUT"/bypass-*.txt && { echo "proxy password printed"; exit 1; }
[ "$off_backend" -gt 0 ] && [ $((r2 - r1)) -ge 3 ] && [ "$on_backend" -eq 0 ] && [ "$on_site" -gt 0 ] \
  && cmp -s "$OUT/geo-off.txt" "$OUT/geo-on.txt"
