#!/usr/bin/env bash
# Build one image, push it to the environment's Azure registry and roll it out
# to its Container App, then wait until the new revision is healthy. The same
# file in every Foji and Tutoria repo; the workflow is already signed in (azure/login, OIDC).
#
#   APP          Container App, e.g. tutoria-ai-api-dev (its identity has the same name)
#   IMAGE_NAME   repository in the registry, e.g. tutoria-ai-api
#   RG, REGISTRY from deploy/<env>.env (REGISTRY: the name, not the login server)
#   PORT         ingress port ('' for no ingress)
#   HEALTH       path that must answer 200 after the rollout ('' to skip)
#   DOCKERFILE   default Dockerfile; CONTEXT default .
#   BUILD_ARGS   optional, one KEY=VALUE per line
#
# Traffic stays on the running revision until the new one is ready (single
# revision mode), so a revision that never gets healthy fails this job and
# leaves the old one serving.
set -euo pipefail

: "${APP:?}" "${IMAGE_NAME:?}" "${RG:?}" "${REGISTRY:?}"
DOCKERFILE=${DOCKERFILE:-Dockerfile}
CONTEXT=${CONTEXT:-.}
PORT=${PORT:-}
HEALTH=${HEALTH:-}
TIMEOUT_SECONDS=${TIMEOUT_SECONDS:-600}
SERVER="$REGISTRY.azurecr.io"
IMAGE="$SERVER/$IMAGE_NAME:$GITHUB_SHA"

echo "::group::Build and push $IMAGE"
args=()
while IFS= read -r line; do
  [ -n "$line" ] && args+=(--build-arg "$line")
done <<<"${BUILD_ARGS:-}"
az acr login --name "$REGISTRY" --only-show-errors
docker build --pull -f "$DOCKERFILE" ${args[@]+"${args[@]}"} -t "$IMAGE" "$CONTEXT"
docker push "$IMAGE"
echo "::endgroup::"

echo "::group::Roll out to $APP"
IDENTITY=$(az identity show -g "$RG" -n "$APP" --query id -o tsv)
# Idempotent. The first deploy replaces the placeholder's public image and port.
# Only when missing: a needless write can collide with an update still running.
if ! az containerapp show -g "$RG" -n "$APP" --query "properties.configuration.registries[].server" -o tsv | grep -qx "$SERVER"; then
  az containerapp registry set -g "$RG" -n "$APP" --server "$SERVER" --identity "$IDENTITY" -o none --only-show-errors
fi
if [ -n "$PORT" ]; then
  current=$(az containerapp show -g "$RG" -n "$APP" --query properties.configuration.ingress.targetPort -o tsv)
  [ "$current" = "$PORT" ] || az containerapp ingress update -g "$RG" -n "$APP" --target-port "$PORT" -o none --only-show-errors
fi
az containerapp update -g "$RG" -n "$APP" --image "$IMAGE" -o none --only-show-errors
REVISION=$(az containerapp show -g "$RG" -n "$APP" --query properties.latestRevisionName -o tsv)
FQDN=$(az containerapp show -g "$RG" -n "$APP" --query properties.configuration.ingress.fqdn -o tsv 2>/dev/null || true)
echo "revision $REVISION"
echo "::endgroup::"

logs() {
  echo "::group::Logs of $REVISION"
  az containerapp logs show -g "$RG" -n "$APP" --revision "$REVISION" --type system --tail 40 2>&1 || true
  az containerapp logs show -g "$RG" -n "$APP" --revision "$REVISION" --type console --tail 80 2>&1 || true
  echo "::endgroup::"
}

deadline=$((SECONDS + TIMEOUT_SECONDS))
unhealthy=0
while :; do
  # One line, then a here-string: read fails on text without a final newline.
  state=$(az containerapp revision show -g "$RG" -n "$APP" --revision "$REVISION" \
    --query "[properties.provisioningState, properties.healthState, properties.runningState]" -o tsv | tr '\n' ' ')
  read -r provisioning health running <<<"$state"
  echo "$(date -u +%T) provisioning=$provisioning health=$health running=$running"
  case "$provisioning" in
    Failed) echo "::error::$REVISION failed to provision"; logs; exit 1 ;;
  esac
  if [ "$health" = "Healthy" ] || { [ "$provisioning" = "Provisioned" ] && [ "$running" = "ScaledToZero" ]; }; then
    break
  fi
  if [ "$health" = "Unhealthy" ]; then
    unhealthy=$((unhealthy + 1))
    [ "$unhealthy" -lt 6 ] || { echo "::error::$REVISION stays unhealthy"; logs; exit 1; }
  fi
  # An app that scales to zero only starts on a request.
  if [ -n "$FQDN" ] && [ -n "$HEALTH" ]; then
    curl -s -o /dev/null --max-time 10 "https://$FQDN$HEALTH" || true
  fi
  [ "$SECONDS" -lt "$deadline" ] || { echo "::error::$REVISION not healthy after ${TIMEOUT_SECONDS}s"; logs; exit 1; }
  sleep 10
done

if [ -n "$FQDN" ] && [ -n "$HEALTH" ]; then
  # Wakes a scaled-to-zero app; up to 2 minutes for its first replica.
  if ! curl -fsS -o /dev/null --retry 12 --retry-delay 10 --retry-all-errors --max-time 20 "https://$FQDN$HEALTH"; then
    echo "::error::https://$FQDN$HEALTH does not answer 200"; logs; exit 1
  fi
  echo "https://$FQDN$HEALTH answers 200"
fi

# Basic registries have no retention policy: keep the newest 10 tags.
az acr repository show-tags -n "$REGISTRY" --repository "$IMAGE_NAME" --orderby time_desc -o tsv \
  | tail -n +11 | while read -r old; do
      az acr repository delete -n "$REGISTRY" --image "$IMAGE_NAME:$old" --yes -o none --only-show-errors || true
    done

{
  echo "## Azure: $APP"
  echo "- Image: \`$IMAGE\`"
  echo "- Revision: \`$REVISION\`"
  [ -z "$FQDN" ] || echo "- URL: https://$FQDN"
} >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
