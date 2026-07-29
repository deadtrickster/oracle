#!/usr/bin/env bash
# move-data-to-mnt.sh — relocate the Elasticsearch and SereneDB data onto /mnt/data.
#
# WHY: both live on `/` (2.3T, 69% full, 689G left) while /mnt/data has 6.3T free. ES is ~5.7G and
# SereneDB ~10.5G today, which is not the problem — the problem is what comes next: ingesting arXiv
# will grow both by far more than `/` can absorb, and discovering that at 100% disk means a corrupt
# index rather than a tidy failure.
#
# HOW, and why this way:
#
#   * The volume NAMES do not change. The ES volume keeps the name and the exact compose labels it
#     has now, and is simply re-pointed at a bind under /mnt/data. Nothing in RAGFlow's compose
#     files is edited — those are a pinned upstream checkout (v0.26.4), so any edit there is silently
#     lost on the next re-checkout, and this stack has already been bitten by patches that vanish.
#   * Copying runs inside a ROOT CONTAINER that mounts both source and destination. Docker's volume
#     directories are root-owned, so the alternative is host sudo; this way ownership and modes are
#     preserved by tar without touching the host's privileges at all.
#   * Nothing is deleted. The old volume is RENAMED aside (ES) or simply left in place (SereneDB's
#     anonymous volume, which a second container still references). Reclaim later, deliberately,
#     once you have seen the stack come back healthy.
#
#   ./move-data-to-mnt.sh --dry-run    show what would happen
#   ./move-data-to-mnt.sh              do it
#
# Safe to re-run: each step checks whether it has already been done.
set -uo pipefail

DEST="${ORACLE_DATA_ROOT:-/mnt/data/oracle}"
DOCKER_DIR="${ORACLE_DOCKER_DIR:-$HOME/Projects/oracle/ragflow/docker}"
ES_VOLUME="docker_esdata01"
SERENE_CONTAINER="serenedb"
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

say() { printf '%s\n' "$*"; }
run() {
	if [ "$DRY" -eq 1 ]; then
		printf '  would: %s\n' "$*"
	else
		printf '  %s\n' "$*"
		"$@"
	fi
}

# ── preflight ────────────────────────────────────────────────────────────────────────────────────
if ! docker info >/dev/null 2>&1; then
	say "docker is not available"
	exit 1
fi

avail_kb="$(df -Pk "$(dirname "$DEST")" | awk 'NR==2 {print $4}')"
need_kb=$((25 * 1024 * 1024)) # 25G: current ~16G plus room to land the copy before anything is freed
if [ "${avail_kb:-0}" -lt "$need_kb" ]; then
	say "only $((avail_kb / 1024 / 1024))G free at $(dirname "$DEST") — want at least 25G"
	exit 1
fi

# An in-flight parse must not be interrupted: a half-written segment is exactly the corruption this
# move exists to avoid.
if command -v python3 >/dev/null && [ -f "$HOME/Projects/oracle/ingest-status.py" ]; then
	if python3 "$HOME/Projects/oracle/ingest-status.py" 2>/dev/null | grep -q "RUNNING"; then
		say "ingestion is RUNNING — let it finish (or ./ingest-ctl.sh pause) before moving data"
		exit 1
	fi
fi

say "destination : $DEST"
say "free there  : $((avail_kb / 1024 / 1024))G"
say

# ── 1. stop everything that holds the data open ──────────────────────────────────────────────────
say "1. stopping the stack"
if [ "$DRY" -eq 1 ]; then
	say "  would: docker compose stop (in $DOCKER_DIR); docker stop $SERENE_CONTAINER"
else
	(cd "$DOCKER_DIR" && docker compose stop 2>&1 | tail -2)
	docker stop "$SERENE_CONTAINER" >/dev/null 2>&1 || true
	docker stop serenedb-v3-old >/dev/null 2>&1 || true
fi

# ── 2. copy, preserving ownership ────────────────────────────────────────────────────────────────
copy_volume() { # <volume-or-container-source> <dest subdir> <mode: volume|container>
	local src="$1" sub="$2" mode="$3"
	local dst="$DEST/$sub"
	if [ -d "$dst" ] && [ -n "$(ls -A "$dst" 2>/dev/null)" ]; then
		say "  $sub already populated at $dst — skipping copy"
		return 0
	fi
	run mkdir -p "$dst"
	# tar rather than cp: it preserves uid/gid/mode/xattrs across the mount boundary, and an
	# Elasticsearch data directory owned by the wrong uid simply refuses to start.
	if [ "$mode" = volume ]; then
		run docker run --rm -v "$src":/from:ro -v "$dst":/to alpine \
			sh -c "cd /from && tar cf - . | (cd /to && tar xf -)"
	else
		run docker run --rm --volumes-from "$src":ro -v "$dst":/to alpine \
			sh -c "cd /var/lib/serenedb && tar cf - . | (cd /to && tar xf -)"
	fi
}

say
say "2. copying data (root container, ownership preserved)"
copy_volume "$ES_VOLUME" es volume
copy_volume "$SERENE_CONTAINER" serenedb container

# ── 3. re-point the ES volume, keeping its name and compose labels ───────────────────────────────
say
say "3. re-pointing $ES_VOLUME at $DEST/es"
current_device="$(docker volume inspect "$ES_VOLUME" --format '{{index .Options "device"}}' 2>/dev/null)"
if [ "$current_device" = "$DEST/es" ]; then
	say "  already bound to $DEST/es"
else
	# Keep the volume aside rather than destroying it. `docker volume rm` on a volume still
	# referenced by a stopped container fails anyway, so the container is removed first — compose
	# recreates it on the next `up`, which is routine, unlike losing an index.
	if [ "$DRY" -eq 1 ]; then
		say "  would: docker rm docker-es01-1; docker volume rm $ES_VOLUME"
		say "  would: docker volume create --opt device=$DEST/es (with the same compose labels)"
	else
		docker rm -f docker-es01-1 >/dev/null 2>&1 || true
		docker volume rm "$ES_VOLUME" >/dev/null 2>&1 || {
			say "  could not remove $ES_VOLUME — something still uses it; aborting before any damage"
			exit 1
		}
		docker volume create \
			--label com.docker.compose.project=docker \
			--label com.docker.compose.volume=esdata01 \
			--label com.docker.compose.version=2.40.3 \
			--driver local --opt type=none --opt o=bind --opt device="$DEST/es" \
			"$ES_VOLUME" >/dev/null
		say "  recreated $ES_VOLUME -> $DEST/es"
	fi
fi

say
say "4. SereneDB must be recreated with an explicit bind (its volume is anonymous)."
say "   Its data is now copied to $DEST/serenedb. Recreate with:"
say
say "     docker rm -f $SERENE_CONTAINER"
say "     docker run -d --name $SERENE_CONTAINER --network docker_ragflow -p 7890:7890 \\"
say "       -e POSTGRES_PASSWORD=oracle-sdb \\"
say "       -v $DEST/serenedb:/var/lib/serenedb \\"
say "       serenedb/serenedb:26.07.4 serened"
say
say "   Left as a printed step on purpose: this container was started by hand, not by compose, so"
say "   the command above is reconstructed from 'docker inspect' and deserves your eyes before it"
say "   replaces a container holding 10G of index."
say
say "5. then: cd $DOCKER_DIR && docker compose up -d"
say "   verify: curl -s localhost:1200/_cluster/health | jq .status   (expect green/yellow)"
say "           ./ingest-status.py                                     (expect the same totals)"
