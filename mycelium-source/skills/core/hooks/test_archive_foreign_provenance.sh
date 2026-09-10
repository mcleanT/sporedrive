#!/usr/bin/env bash
# Unit tests for mycelium_archive_foreign_provenance (mycelium-hook-lib.sh).
#
# The helper preserves a deferred second-root-writer's per-edit provenance
# BEFORE the accepting owner's Stop cleanup removes the transient ledger
# (mycelium-foreign-activity.tmp). Accepted finding:
# work/lifecycle-deferred-evidence-probe.json.
#
# Design under test: a bounded DURABLE-JOURNAL HANDOFF. The preservation
# primitive is a single atomic rename of the ledger to a uniquely named journal
# (mycelium-foreign-activity.journal.XXXXXX) that is NEVER unlinked while it
# holds foreign rows and NEVER restored over the ledger path; all session-tagged
# rows are kept verbatim (a foreign-only view is derived, never the source).
#
# Each composed case below is a green-after pin for a loss mode reproduced
# against an earlier implementation and recorded in the retained probes:
#   work/archive-helper-review-probes.json (first review):
#     - awk_failure: read failure swallowed -> only ledger deleted, nothing kept
#     - archive_symlink: symlinked append target followed into an unrelated file
#     - late_append: append after snapshot, before delete, lost
#   work/archive-handoff-review-probes.json (composed review):
#     - read_failure_plus_late_append: restore over a fresh ledger lost the new row
#     - preopened_writer_fd: unlinking the claimed inode lost a pre-opened-fd row
#     - existing_claim_collision: a reused-name claim clobbered a prior attempt
#
# Every case drives the helper through the SAME contract the three
# mycelium-stop-check.sh cleanup sites use: `helper || true`, and the caller
# NEVER deletes the ledger (the helper owns disposal).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$HERE/mycelium-hook-lib.sh"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
PASS_COUNT=0; FAIL_COUNT=0
pass() { echo -e "${GREEN}PASS${NC} — $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo -e "${RED}FAIL${NC} — $1"; echo -e "       ${YELLOW}$2${NC}"; FAIL_COUNT=$((FAIL_COUNT + 1)); }

L="mycelium-foreign-activity.tmp"
JGLOB="mycelium-foreign-activity.journal."

caller() { mycelium_archive_foreign_provenance "$1" "$2" || true; }
journals() { ls "$1"/${JGLOB}* 2>/dev/null; }
# Is $2 (a fixed string, ^anchored regex) preserved anywhere durable in $1 --
# i.e. in a journal file OR in a freshly recreated ledger?
preserved() {
  grep -qs -- "$2" "$1"/${JGLOB}* "$1/$L" 2>/dev/null && echo yes || echo no
}

# ── normal ───────────────────────────────────────────────────────────────
echo "CASE normal: all rows preserved verbatim in a durable journal, ledger disposed"
t=$(mktemp -d)
printf 'writer-B early.txt\nowner-A own.txt\nwriter-B second.txt\n' > "$t/$L"
printf 'unrelated original\n' > "$t/bystander.txt"
caller "$t" "owner-A"
j=$(journals "$t")
if [ -e "$t/$L" ]; then
  fail "normal — ledger must be disposed after handoff" "ledger still present"
elif [ -z "$j" ]; then
  fail "normal — a durable journal must be created for foreign provenance" "no journal"
elif ! grep -q '^writer-B early.txt$' $j || ! grep -q '^writer-B second.txt$' $j; then
  fail "normal — foreign rows must be preserved verbatim" "journal: $(cat $j)"
elif ! grep -q '^owner-A own.txt$' $j; then
  fail "normal — the journal keeps ALL session-tagged rows verbatim (not a lossy foreign-only filter)" "journal: $(cat $j)"
elif [ "$(cat "$t/bystander.txt")" != "unrelated original" ]; then
  fail "normal — an unrelated file must never be touched" "bystander: $(cat "$t/bystander.txt")"
else
  pass "normal — durable journal holds all rows verbatim, ledger disposed, bystander untouched"
fi
rm -rf "$t"

# ── awk_failure (first review) + read_failure_plus_late_append (composed) ─
echo "CASE read_failure_plus_late_append: a read failure forces preservation and NEVER restores over a fresh ledger"
t=$(mktemp -d)
printf 'writer-B early.txt\n' > "$t/$L"
chmod 000 "$t/$L"                              # awk cannot read -> classification read fails
caller "$t" "owner-A"                          # must hand off (preserve), never discard, never restore
printf 'writer-C late.txt\n' >> "$t/$L"        # a fresh append lands in a brand-new ledger
chmod 644 "$t"/${JGLOB}* 2>/dev/null || true
early=$(preserved "$t" '^writer-B early.txt$')
late=$(preserved "$t" '^writer-C late.txt$')
if [ "$early" != yes ]; then
  fail "read_failure — the unread ledger must be preserved, never discarded" "early lost (original defect: read failure read as empty)"
elif [ "$late" != yes ]; then
  fail "read_failure_plus_late_append — a fresh late append must survive (no restore-over-fresh-ledger)" "late lost"
else
  pass "read_failure_plus_late_append — read failure preserved the ledger AND the racing fresh append survived"
fi
rm -rf "$t"

# ── preopened_writer_fd (composed) ───────────────────────────────────────
echo "CASE preopened_writer_fd: a writer holding a pre-rename fd keeps writing into a journal that is never unlinked"
t=$(mktemp -d)
printf 'writer-B early.txt\n' > "$t/$L"
exec 9>>"$t/$L"                                # fd opened BEFORE the helper
caller "$t" "owner-A"                          # foreign present -> hand off; fd 9 now targets the journal inode
printf 'writer-C late.txt\n' >&9               # append via the pre-opened fd, after the snapshot
exec 9>&-
early=$(preserved "$t" '^writer-B early.txt$')
late=$(preserved "$t" '^writer-C late.txt$')
if [ "$early" != yes ] || [ "$late" != yes ]; then
  fail "preopened_writer_fd — both the snapshot row and the pre-opened-fd late row must survive (journal never unlinked)" "early=$early late=$late (original defect: rm claimed unlinked the inode)"
else
  pass "preopened_writer_fd — journal never unlinked, so the pre-opened-fd late write is preserved"
fi
rm -rf "$t"

# ── existing_claim_collision (composed) ──────────────────────────────────
echo "CASE existing_claim_collision: a prior attempt's journal is never reused or clobbered"
t=$(mktemp -d)
printf 'writer-B early.txt\n' > "$t/$L"
printf 'writer-Z prior.txt\n' > "$t/${JGLOB}PRIORFIXED"   # a prior attempt's durable journal
caller "$t" "owner-A"
if [ ! -f "$t/${JGLOB}PRIORFIXED" ] || ! grep -q '^writer-Z prior.txt$' "$t/${JGLOB}PRIORFIXED"; then
  fail "existing_claim_collision — a prior journal must never be deleted or clobbered" "prior journal lost (original defect: reused \$\$ name rm'd on entry)"
elif [ "$(preserved "$t" '^writer-B early.txt$')" != yes ]; then
  fail "existing_claim_collision — the new foreign provenance must still be preserved alongside the prior one" "new lost"
else
  pass "existing_claim_collision — unique name; prior journal untouched, new provenance preserved"
fi
rm -rf "$t"

# ── archive_symlink surface eliminated ───────────────────────────────────
echo "CASE archive_symlink_surface_gone: preservation is a rename to a fresh unique file, so there is no attacker-influenced append target to follow"
t=$(mktemp -d)
printf 'writer-B early.txt\n' > "$t/$L"
printf 'unrelated original\n' > "$t/secret.txt"
# A pre-existing symlink at the OLD archive path must be irrelevant now.
ln -s "$t/secret.txt" "$t/mycelium-foreign-activity-archive.log"
caller "$t" "owner-A"
if [ "$(cat "$t/secret.txt")" != "unrelated original" ]; then
  fail "archive_symlink_surface_gone — nothing may be written through any pre-existing symlink" "secret.txt: $(cat "$t/secret.txt")"
elif [ "$(preserved "$t" '^writer-B early.txt$')" != yes ]; then
  fail "archive_symlink_surface_gone — foreign provenance must still be preserved in the journal" "not preserved"
else
  pass "archive_symlink_surface_gone — no append target to follow; unrelated file untouched, provenance in journal"
fi
rm -rf "$t"

# ── owner_only_interleave (corrected owner-only finding) ─────────────────
# The ledger starts owner-ONLY. A foreign append's redirection opens the inode
# BEFORE cleanup runs, and its per-line write lands AFTER. An owner-only unlink
# path freed that inode and lost the write
# (work/journal-owner-only-interleaving-probe-corrected.json). With no unlink
# path at all, the handed-off inode survives and the late foreign write is kept.
echo "CASE owner_only_interleave: a foreign append opened before cleanup, written after, must survive even on an owner-only ledger (no unlink path)"
t=$(mktemp -d)
printf 'owner-A own.txt\n' > "$t/$L"          # owner-ONLY at classification time
exec 8>>"$t/$L"                                # a foreign appender opens the inode BEFORE cleanup
caller "$t" "owner-A"                          # handoff; must NOT unlink even though it looked owner-only
printf 'writer-C late.txt\n' >&8               # the per-line write lands after the handoff
exec 8>&-
if [ "$(preserved "$t" '^writer-C late.txt$')" != yes ]; then
  fail "owner_only_interleave — a foreign write opened-before/written-after must survive; an owner-only ledger must never take an unlink path" "late foreign row lost (the corrected owner-only defect)"
elif [ "$(preserved "$t" '^owner-A own.txt$')" != yes ]; then
  fail "owner_only_interleave — the owner's own row must be preserved verbatim in the journal too" "owner row lost"
else
  pass "owner_only_interleave — no unlink path: the handed-off inode survived, preserving both the owner row and the interleaved foreign write"
fi
rm -rf "$t"

# ── self_only ────────────────────────────────────────────────────────────
# A pure owner-only ledger is now ALSO handed off to a durable journal rather
# than unlinked: distinguishing "owner-only forever" from "owner-only with an
# in-flight foreign append" cannot be done without a read that races the
# lock-free appender, so a small raw journal is retained rather than risk loss.
echo "CASE self_only: an owner-only ledger is handed off to a durable journal (retained, never unlinked)"
t=$(mktemp -d)
printf 'owner-A a.txt\nowner-A b.txt\n' > "$t/$L"
caller "$t" "owner-A"
j=$(journals "$t")
if [ -e "$t/$L" ]; then
  fail "self_only — the transient ledger must be handed off (moved), not left in place" "ledger still at original path"
elif [ -z "$j" ]; then
  fail "self_only — the ledger must be preserved in a durable journal, never unlinked" "no journal"
elif ! grep -q '^owner-A a.txt$' $j || ! grep -q '^owner-A b.txt$' $j; then
  fail "self_only — the owner-only rows must be preserved verbatim" "journal: $(cat $j)"
else
  pass "self_only — owner-only ledger handed off to a durable journal (retained, not unlinked)"
fi
rm -rf "$t"

# ── empty / absent / symlink_ledger ──────────────────────────────────────
# An empty ledger is still handed off (never unlinked): it may be the O_APPEND
# window of a foreign write that opened the inode but has not written yet.
echo "CASE empty_ledger: handed off to a journal (never unlinked -- may be an in-flight append window)"
t=$(mktemp -d); : > "$t/$L"; caller "$t" "owner-A"
if [ -e "$t/$L" ]; then
  fail "empty_ledger — the empty ledger must be handed off (moved), not left in place" "ledger still at original path"
elif [ -z "$(journals "$t")" ]; then
  fail "empty_ledger — an empty ledger is handed off (never unlinked), so a journal exists" "no journal"
else pass "empty_ledger — handed off to a journal (never unlinked)"; fi
rm -rf "$t"

echo "CASE absent_ledger: safe no-op"
t=$(mktemp -d); caller "$t" "owner-A"
if [ -n "$(journals "$t")" ]; then fail "absent_ledger — no journal when there is no ledger" "journal created"; else pass "absent_ledger — safe no-op"; fi
rm -rf "$t"

echo "CASE symlink_ledger: neither followed nor renamed/deleted-through"
t=$(mktemp -d); printf 'writer-B via.txt\n' > "$t/real.txt"; ln -s "$t/real.txt" "$t/$L"; caller "$t" "owner-A"
if [ ! -e "$t/real.txt" ] || [ "$(cat "$t/real.txt")" != "writer-B via.txt" ]; then
  fail "symlink_ledger — the symlink target must be left intact" "target changed"
elif [ -n "$(journals "$t")" ]; then
  fail "symlink_ledger — a symlinked ledger must not be handed off" "journal created via symlink"
else pass "symlink_ledger — symlinked ledger neither followed nor handed off; target intact"; fi
rm -rf "$t"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "Results: ${GREEN}${PASS_COUNT} passed${NC} / ${RED}${FAIL_COUNT} failed${NC} / $((PASS_COUNT + FAIL_COUNT)) total"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
[ "$FAIL_COUNT" -eq 0 ]
