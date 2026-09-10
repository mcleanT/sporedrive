#!/usr/bin/env bash
# Focused tests for scripts/repoint_claude_project_hooks.py (r2 seq65 activation;
# seq81 whole-tree fail-closed baseline).
#
# seq81 supersedes the earlier per-handler/ordinal resolver (seq75/seq77 fixes):
# rollback now compares the ENTIRE `hooks` subtree against two receipt snapshots
# (pre_hooks, post_hooks). ANY later hooks-tree change -- an added hook, a
# prepended group (same or different matcher), or drift on the owned handler
# itself -- makes current != post_hooks AND current != pre_hooks, so rollback
# REFUSES with no writes. This is deliberate: the old "resolver" design tried to
# guess which handler was "the" owned one and could misattribute; the new
# contract never guesses. seq75/seq77/seq79 below reproduce the exact scenarios
# Codex found broken in the resolver design and assert the new refuse-with-no-
# writes behavior.
#
# All fixtures are temp files OUTSIDE any repository.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(cd "$HERE/../.." && pwd)"
TOOL="$REPO/scripts/repoint_claude_project_hooks.py"
OLD='/Users/mst36/.claude/mycelium-runtime/skills/core/hooks/'
NEW='/Users/mst36/.claude/mycelium-runtime-wfi/skills/core/hooks/'
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0
ok(){ pass=$((pass+1)); echo "  PASS $1"; }
bad(){ fail=$((fail+1)); echo "  FAIL $1"; }
md5of(){ md5 -q "$1" 2>/dev/null || md5sum "$1" | cut -d' ' -f1; }

# Build a fixture with the 6 owned core hooks (OLD prefix) + one UNRELATED non-mycelium hook.
mkfix(){ python3 - "$1" "$OLD" <<'PY'
import json,sys
out,old=sys.argv[1],sys.argv[2]
s={"permissions":{"allow":["Bash"]},
   "hooks":{
     "SessionStart":[{"hooks":[{"type":"command","command":old+"mycelium-health.sh"}]}],
     "PostToolUse":[
        {"matcher":"Edit|Write","hooks":[{"type":"command","command":old+"mycelium-activity-tracker.sh"}]},
        {"matcher":"Bash","hooks":[{"type":"command","command":old+"mycelium-post-action.sh"},
                                    {"type":"command","command":old+"mycelium-data-tracker.sh"},
                                    {"type":"command","command":"/usr/local/bin/unrelated-thirdparty-hook.sh"}]},
        {"matcher":"Read","hooks":[{"type":"command","command":old+"mycelium-read-tracker.sh"}]}],
     "Stop":[{"hooks":[{"type":"command","command":old+"mycelium-stop-check.sh"}]}]}}
open(out,"w").write(json.dumps(s,indent=2))
PY
}
cnt(){ local n; n="$(grep -c "$2" "$1" 2>/dev/null)"; echo "${n:-0}"; }

# 1) apply repoints exactly 6, preserves mode 0600, valid JSON, unrelated hook intact
F="$TMP/f1.json"; mkfix "$F"; chmod 600 "$F"
python3 "$TOOL" apply "$F" --old-prefix "$OLD" --new-prefix "$NEW" --expect 6 --receipt "$TMP/r1.json" >/dev/null 2>&1
rc=$?
[ "$rc" = 0 ] && [ "$(cnt "$F" 'mycelium-runtime-wfi/skills')" = 6 ] && [ "$(cnt "$F" 'mycelium-runtime/skills/core/hooks/mycelium')" = 0 ] \
  && [ "$(stat -f '%A' "$F")" = 600 ] && grep -q 'unrelated-thirdparty-hook' "$F" && python3 -c "import json;json.load(open('$F'))" \
  && ok "apply: 6 repointed, mode 0600 preserved, unrelated hook + json intact" || bad "apply basic"

# 2) --expect mismatch refuses (no write)
F2="$TMP/f2.json"; mkfix "$F2"
python3 "$TOOL" apply "$F2" --old-prefix "$OLD" --new-prefix "$NEW" --expect 5 --receipt "$TMP/r2.json" >/dev/null 2>&1
[ "$?" = 1 ] && [ "$(cnt "$F2" 'mycelium-runtime-wfi')" = 0 ] && ok "apply: --expect mismatch refuses, no write" || bad "apply expect-mismatch"

# 3) clean restore: rollback restores whole tree -> parsed-equal to original
F3="$TMP/f3.json"; mkfix "$F3"; cp "$F3" "$TMP/f3.orig"
python3 "$TOOL" apply "$F3" --old-prefix "$OLD" --new-prefix "$NEW" --expect 6 --receipt "$TMP/r3.json" >/dev/null 2>&1
python3 "$TOOL" rollback --receipt "$TMP/r3.json" >/dev/null 2>&1; rc=$?
[ "$rc" = 0 ] && python3 -c "import json,sys; sys.exit(0 if json.load(open('$F3'))==json.load(open('$TMP/f3.orig')) else 1)" \
  && ok "rollback: clean round-trip parsed-equal to original" || bad "rollback round-trip"

# 4) idempotent no-op: rollback again on a fully-restored file -> exit 0 no-op, no writes
F4="$TMP/f4.json"; mkfix "$F4"
python3 "$TOOL" apply "$F4" --old-prefix "$OLD" --new-prefix "$NEW" --expect 6 --receipt "$TMP/r4.json" >/dev/null 2>&1
python3 "$TOOL" rollback --receipt "$TMP/r4.json" >/dev/null 2>&1
before="$(md5of "$F4")"
out="$(python3 "$TOOL" rollback --receipt "$TMP/r4.json" 2>&1)"; rc=$?
after="$(md5of "$F4")"
[ "$rc" = 0 ] && echo "$out" | grep -qi 'NO-OP' && [ "$before" = "$after" ] \
  && ok "rollback: idempotent no-op on already-restored file, no writes" || bad "rollback idempotent no-op"

# --- seq75 (whole-tree): a later hooks-tree change invalidates the receipt ---

# 5) seq75-a [SUPERSEDED->REFUSE]: apply changes ONE health hook; a later UNRELATED
# PostToolUse hook using the SAME new runtime prefix is added afterward. Under the
# whole-tree contract this is no longer "preserved and still rolled back" (that was
# the seq75-a expectation under the retired per-handler resolver) -- the current
# tree now differs from BOTH pre_hooks and post_hooks, so rollback must REFUSE with
# no writes.
mkhealth(){ python3 - "$1" "$OLD" <<'PY'
import json,sys
out,old=sys.argv[1],sys.argv[2]
s={"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":old+"mycelium-health.sh"}]}]}}
open(out,"w").write(json.dumps(s,indent=2))
PY
}
F5="$TMP/f5.json"; mkhealth "$F5"
python3 "$TOOL" apply "$F5" --old-prefix "$OLD" --new-prefix "$NEW" --expect 1 --receipt "$TMP/r5.json" >/dev/null 2>&1
python3 - "$F5" "$NEW" <<'PY'
import json,sys
f,new=sys.argv[1],sys.argv[2]
d=json.load(open(f))
d.setdefault("hooks",{}).setdefault("PostToolUse",[]).append(
    {"matcher":"Bash","hooks":[{"type":"command","command":new+"some-other-later-hook.sh"}]})
open(f,"w").write(json.dumps(d,indent=2))
PY
before="$(md5of "$F5")"
python3 "$TOOL" rollback --receipt "$TMP/r5.json" >/dev/null 2>&1; rc=$?
after="$(md5of "$F5")"
[ "$rc" = 1 ] && [ "$before" = "$after" ] \
  && ok "seq75: later new-prefix hook addition -> whole tree mismatch -> REFUSE, no writes" || bad "seq75 refuse-on-later-addition"

# 6) seq75-b: original owned hook drifts to a THIRD runtime -> must REFUSE (not a
# false "successful NO-OP", which was the reported regression).
F6="$TMP/f6.json"; mkhealth "$F6"
python3 "$TOOL" apply "$F6" --old-prefix "$OLD" --new-prefix "$NEW" --expect 1 --receipt "$TMP/r6.json" >/dev/null 2>&1
python3 - "$F6" <<'PY'
import json,sys
f=sys.argv[1]; d=json.load(open(f))
d["hooks"]["SessionStart"][0]["hooks"][0]["command"]="/Users/mst36/.claude/mycelium-runtime-THIRD/skills/core/hooks/mycelium-health.sh"
open(f,"w").write(json.dumps(d,indent=2))
PY
before="$(md5of "$F6")"
out="$(python3 "$TOOL" rollback --receipt "$TMP/r6.json" 2>&1)"; rc=$?
after="$(md5of "$F6")"
[ "$rc" = 1 ] && [ "$before" = "$after" ] && ! echo "$out" | grep -qi 'NO-OP' \
  && ok "seq75-b: owned hook drifted to third runtime -> REFUSE (not a false no-op)" || bad "seq75-b drift-refuse"

# --- seq77: prepending a DIFFERENT-matcher group with the same post command ---

mkstartup(){ python3 - "$1" "$OLD" <<'PY'
import json,sys
out,old=sys.argv[1],sys.argv[2]
s={"hooks":{"SessionStart":[{"matcher":"startup","hooks":[{"type":"command","command":old+"mycelium-health.sh"}]}]}}
open(out,"w").write(json.dumps(s,indent=2))
PY
}
prepend_group(){ python3 - "$1" "$2" "$3" <<'PY'
import json,sys
f,matcher,cmd=sys.argv[1],sys.argv[2],sys.argv[3]
d=json.load(open(f))
group={"matcher":matcher,"hooks":[{"type":"command","command":cmd}]}
d["hooks"]["SessionStart"].insert(0,group)
open(f,"w").write(json.dumps(d,indent=2))
PY
}
health_post="${NEW}mycelium-health.sh"

# 7) seq77: prepend a matcher=resume group with the identical post command. The
# old per-handler resolver misattributed this by (event, matcher-value-search,
# command-value); it no longer matters here -- the tree changed (a group was
# inserted), so rollback REFUSES regardless of matcher/value coincidences.
F7="$TMP/f7.json"; mkstartup "$F7"
python3 "$TOOL" apply "$F7" --old-prefix "$OLD" --new-prefix "$NEW" --expect 1 --receipt "$TMP/r7.json" >/dev/null 2>&1
prepend_group "$F7" "resume" "$health_post"
before="$(md5of "$F7")"
python3 "$TOOL" rollback --receipt "$TMP/r7.json" >/dev/null 2>&1; rc=$?
after="$(md5of "$F7")"
[ "$rc" = 1 ] && [ "$before" = "$after" ] \
  && ok "seq77: prepended resume-matcher group -> whole tree mismatch -> REFUSE, no writes" || bad "seq77 refuse-on-prepend"

# 8) seq79: prepend a group with the SAME matcher ("startup") but a different
# timeout, holding the recorded post command. Even a same-matcher, same-command
# duplicate does not fool the whole-tree comparison -- the tree changed, refuse.
F8="$TMP/f8.json"; mkstartup "$F8"
python3 "$TOOL" apply "$F8" --old-prefix "$OLD" --new-prefix "$NEW" --expect 1 --receipt "$TMP/r8.json" >/dev/null 2>&1
python3 - "$F8" "$health_post" <<'PY'
import json,sys
f,cmd=sys.argv[1],sys.argv[2]
d=json.load(open(f))
group={"matcher":"startup","hooks":[{"type":"command","command":cmd,"timeout":60}]}
d["hooks"]["SessionStart"].insert(0,group)
open(f,"w").write(json.dumps(d,indent=2))
PY
# also drift the ORIGINAL startup handler to a third runtime, matching the seq79 report
python3 - "$F8" <<'PY'
import json,sys
f=sys.argv[1]; d=json.load(open(f))
for g in d["hooks"]["SessionStart"]:
    if g.get("matcher")=="startup" and g["hooks"][0].get("timeout") is None:
        g["hooks"][0]["command"]="/Users/mst36/.claude/mycelium-runtime-THIRD/skills/core/hooks/mycelium-health.sh"
open(f,"w").write(json.dumps(d,indent=2))
PY
before="$(md5of "$F8")"
python3 "$TOOL" rollback --receipt "$TMP/r8.json" >/dev/null 2>&1; rc=$?
after="$(md5of "$F8")"
[ "$rc" = 1 ] && [ "$before" = "$after" ] \
  && ok "seq79: prepended same-matcher timeout-60 duplicate + drifted original -> REFUSE, no writes" || bad "seq79 refuse-on-same-matcher-duplicate"

# 9) legacy (schema-1, no pre_hooks/post_hooks) receipt passed directly to rollback -> refused with guidance
echo '{"config_path":"/tmp/doesnotmatter.json","old_prefix":"'"$OLD"'","new_prefix":"'"$NEW"'","changed_commands":[]}' > "$TMP/legacy.json"
out="$(python3 "$TOOL" rollback --receipt "$TMP/legacy.json" 2>&1)"; rc=$?
[ "$rc" = 1 ] && echo "$out" | grep -qi 'LEGACY RECEIPT' && ok "legacy schema-1 receipt refused by rollback with reconcile guidance" || bad "legacy receipt refusal"

# --- reconcile: require + verify --from-receipt before a truthful current-state recovery ---

# 10) nonexistent --from-receipt is refused
F10="$TMP/f10.json"; mkfix "$F10"
python3 "$TOOL" apply "$F10" --old-prefix "$OLD" --new-prefix "$NEW" --expect 6 --receipt "$TMP/r10-legacystyle.json" >/dev/null 2>&1
out="$(python3 "$TOOL" reconcile "$F10" --old-prefix "$OLD" --new-prefix "$NEW" --receipt "$TMP/r10v2.json" --from-receipt "$TMP/does-not-exist.json" 2>&1)"; rc=$?
[ "$rc" = 1 ] && [ ! -f "$TMP/r10v2.json" ] && echo "$out" | grep -qi 'does not exist' \
  && ok "reconcile: nonexistent --from-receipt refused, no write" || bad "reconcile nonexistent-from-receipt"

# 10b) missing --from-receipt entirely (and no --recovery) is refused
out="$(python3 "$TOOL" reconcile "$F10" --old-prefix "$OLD" --new-prefix "$NEW" --receipt "$TMP/r10v2b.json" 2>&1)"; rc=$?
[ "$rc" = 1 ] && [ ! -f "$TMP/r10v2b.json" ] && echo "$out" | grep -qi 'required' \
  && ok "reconcile: missing --from-receipt refused, no write" || bad "reconcile missing-from-receipt"

# 11) a legacy receipt that does NOT describe the current config (owned-set mismatch:
# fabricated with one command missing) is refused
F11="$TMP/f11.json"; mkfix "$F11"
python3 "$TOOL" apply "$F11" --old-prefix "$OLD" --new-prefix "$NEW" --expect 6 --receipt "$TMP/r11-unused.json" >/dev/null 2>&1
python3 - "$F11" "$OLD" <<'PY' > "$TMP/legacy11.json"
import json,sys
f,old=sys.argv[1],sys.argv[2]
json.load(open(f))  # sanity
print(json.dumps({
  "config_path": f,
  "old_prefix": old,
  "new_prefix": "/Users/mst36/.claude/mycelium-runtime-wfi/skills/core/hooks/",
  "changed_commands": [old+"mycelium-health.sh"],  # deliberately incomplete: real set has 6
  "pre_sha256": "deadbeef",
  "mode": "0o644",
}, indent=2))
PY
out="$(python3 "$TOOL" reconcile "$F11" --old-prefix "$OLD" --new-prefix "$NEW" --receipt "$TMP/r11v2.json" --from-receipt "$TMP/legacy11.json" 2>&1)"; rc=$?
[ "$rc" = 1 ] && [ ! -f "$TMP/r11v2.json" ] && echo "$out" | grep -qi 'owned-set mismatch' \
  && ok "reconcile: owned-set mismatch against legacy receipt refused, no write" || bad "reconcile owned-set-mismatch"

# 11b) config_path mismatch is refused
python3 - "$F11" "$OLD" <<'PY' > "$TMP/legacy11b.json"
import json,sys
old=sys.argv[2]
print(json.dumps({
  "config_path": "/some/other/path/settings.local.json",
  "old_prefix": old,
  "new_prefix": "/Users/mst36/.claude/mycelium-runtime-wfi/skills/core/hooks/",
  "changed_commands": [],
  "pre_sha256": "deadbeef",
  "mode": "0o644",
}, indent=2))
PY
out="$(python3 "$TOOL" reconcile "$F11" --old-prefix "$OLD" --new-prefix "$NEW" --receipt "$TMP/r11cv2.json" --from-receipt "$TMP/legacy11b.json" 2>&1)"; rc=$?
[ "$rc" = 1 ] && [ ! -f "$TMP/r11cv2.json" ] && echo "$out" | grep -qi 'config_path mismatch' \
  && ok "reconcile: config_path mismatch against legacy receipt refused, no write" || bad "reconcile config-path-mismatch"

# 12) a legacy receipt that DOES faithfully describe the current config verifies and
# upgrades cleanly; the resulting schema-2 receipt then rolls back cleanly
F12="$TMP/f12.json"; mkfix "$F12"; cp "$F12" "$TMP/f12.orig"
python3 "$TOOL" apply "$F12" --old-prefix "$OLD" --new-prefix "$NEW" --expect 6 --receipt "$TMP/r12-legacy.json" >/dev/null 2>&1
# r12-legacy.json IS already schema-2 (this tool always writes schema-2 now); build a
# genuine schema-1-shaped legacy receipt (as real production receipts are) describing
# the same repointed config, to exercise the upgrade path faithfully.
python3 - "$F12" "$OLD" "$NEW" <<'PY' > "$TMP/legacy12.json"
import json,os,sys
f,old,new=sys.argv[1],sys.argv[2],sys.argv[3]
f=os.path.realpath(f)  # match what the tool itself would have recorded (realpath'd)
own=["mycelium-activity-tracker.sh","mycelium-data-tracker.sh","mycelium-health.sh",
     "mycelium-post-action.sh","mycelium-read-tracker.sh","mycelium-stop-check.sh"]
print(json.dumps({
  "config_path": f,
  "old_prefix": old,
  "new_prefix": new,
  "pre_sha256": "irrelevant-for-verification",
  "changed_commands": [old+s for s in own],
  "mode": "0o644",
}, indent=2))
PY
out="$(python3 "$TOOL" reconcile "$F12" --old-prefix "$OLD" --new-prefix "$NEW" --expect 6 --receipt "$TMP/r12v2.json" --from-receipt "$TMP/legacy12.json" 2>&1)"; rc=$?
rc2=1
# seq98: a verified reconcile emits a TRUTHFUL current-state-recovery receipt
# (prints "RECOVERED ...", receipt kind "current-state-recovery") -- never the
# old "RECONCILED"/historical-upgrade label, since a schema-1 receipt never
# captured a tree that could be reconstructed as history.
if [ "$rc" = 0 ] && [ -f "$TMP/r12v2.json" ] && echo "$out" | grep -qi 'RECOVERED' \
   && grep -qi 'current-state-recovery' "$TMP/r12v2.json"; then
  python3 "$TOOL" rollback --receipt "$TMP/r12v2.json" >/dev/null 2>&1
  python3 -c "import json,sys; sys.exit(0 if json.load(open('$F12'))==json.load(open('$TMP/f12.orig')) else 1)"
  rc2=$?
fi
[ "$rc2" = 0 ] && ok "reconcile: verified current-state-recovery succeeds and rolls back cleanly" || bad "reconcile valid-recovery+rollback"

# 13) seq98: --recovery does NOT bypass --from-receipt. Without a prior receipt it
# REFUSES with no write (obsolete pre-seq98 behavior was to write a receipt with no
# legacy receipt consulted); WITH a verified legacy receipt it writes a clearly-
# labeled current-state-recovery receipt that also rolls back cleanly.
F13="$TMP/f13.json"; mkfix "$F13"; cp "$F13" "$TMP/f13.orig"
python3 "$TOOL" apply "$F13" --old-prefix "$OLD" --new-prefix "$NEW" --expect 6 --receipt "$TMP/r13-orig.json" >/dev/null 2>&1
# 13a) --recovery alone (no --from-receipt) is refused, no write
out="$(python3 "$TOOL" reconcile "$F13" --old-prefix "$OLD" --new-prefix "$NEW" --expect 6 --receipt "$TMP/r13v2a.json" --recovery 2>&1)"; rc=$?
reject_ok=0
[ "$rc" = 1 ] && [ ! -f "$TMP/r13v2a.json" ] && echo "$out" | grep -qi 'from-receipt' && reject_ok=1
# 13b) --recovery WITH a verified legacy receipt writes a current-state-recovery receipt
python3 - "$F13" "$OLD" "$NEW" <<'PY' > "$TMP/legacy13.json"
import json,os,sys
f,old,new=sys.argv[1],sys.argv[2],sys.argv[3]
f=os.path.realpath(f)
own=["mycelium-activity-tracker.sh","mycelium-data-tracker.sh","mycelium-health.sh",
     "mycelium-post-action.sh","mycelium-read-tracker.sh","mycelium-stop-check.sh"]
print(json.dumps({
  "config_path": f, "old_prefix": old, "new_prefix": new,
  "pre_sha256": "irrelevant-for-verification",
  "changed_commands": [old+s for s in own], "mode": "0o644",
}, indent=2))
PY
out="$(python3 "$TOOL" reconcile "$F13" --old-prefix "$OLD" --new-prefix "$NEW" --expect 6 --receipt "$TMP/r13v2b.json" --from-receipt "$TMP/legacy13.json" --recovery 2>&1)"; rc=$?
rc2=1
if [ "$reject_ok" = 1 ] && [ "$rc" = 0 ] && [ -f "$TMP/r13v2b.json" ] && grep -qi 'current-state-recovery' "$TMP/r13v2b.json"; then
  python3 "$TOOL" rollback --receipt "$TMP/r13v2b.json" >/dev/null 2>&1
  python3 -c "import json,sys; sys.exit(0 if json.load(open('$F13'))==json.load(open('$TMP/f13.orig')) else 1)"
  rc2=$?
fi
[ "$rc2" = 0 ] && ok "reconcile --recovery: refuses without --from-receipt; with a verified legacy receipt writes a current-state-recovery receipt that rolls back cleanly" || bad "reconcile recovery (seq98 contract)"

echo "repoint-claude-hooks: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
