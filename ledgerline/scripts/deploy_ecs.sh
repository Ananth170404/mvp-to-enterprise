#!/usr/bin/env bash
# Deploy one image to an ECS environment (guide Chapter 2):
#
#   1. register new task definitions: the latest ones, with only the image swapped
#   2. run the migration task and wait; stop here if it fails
#   3. roll the service to the new version and wait until it is stable
#   4. check the new version is really running (not rolled back)
#
# Migrations run BEFORE the new code, so they must be backward compatible:
# the old version keeps serving while they run (expand/contract, guide 2.6).
#
# Usage (CI sets these):
#   IMAGE=<registry>/ledgerline:<sha> CLUSTER=ledgerline-staging scripts/deploy_ecs.sh
set -euo pipefail

: "${IMAGE:?set IMAGE to the full image reference}"
: "${CLUSTER:?set CLUSTER}"
SERVICE="${SERVICE:-$CLUSTER-app}"
APP_FAMILY="${APP_FAMILY:-$CLUSTER-app}"
MIGRATE_FAMILY="${MIGRATE_FAMILY:-$CLUSTER-migrate}"
DESIRED_COUNT="${DESIRED_COUNT:-1}"

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

# Copy the latest revision of a family, swap the image, register it.
# Terraform owns everything else in the definition (settings, secrets, sizes).
register() {
  local family="$1"
  aws ecs describe-task-definition --task-definition "$family" --query taskDefinition --output json \
    | jq --arg image "$IMAGE" '
        .containerDefinitions[0].image = $image
        | del(.taskDefinitionArn, .revision, .status, .requiresAttributes,
              .compatibilities, .registeredAt, .registeredBy, .deregisteredAt)' \
    > "$workdir/$family.json"
  aws ecs register-task-definition --cli-input-json "file://$workdir/$family.json" \
    --query taskDefinition.taskDefinitionArn --output text
}

echo "::group::Register task definitions for $IMAGE"
migrate_td="$(register "$MIGRATE_FAMILY")"
app_td="$(register "$APP_FAMILY")"
echo "migrate: $migrate_td"
echo "app:     $app_td"
echo "::endgroup::"

# The migration task runs in the same subnets and security group as the app,
# so it can reach the database. Read them from the service: one source of truth.
network="$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
  --query 'services[0].networkConfiguration' --output json)"

echo "::group::Run migrations"
task="$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$migrate_td" \
  --launch-type FARGATE --network-configuration "$network" \
  --started-by "deploy-${GITHUB_SHA:-manual}" \
  --query 'tasks[0].taskArn' --output text)"
if [[ -z "$task" || "$task" == "None" ]]; then
  echo "::error::The migration task could not be started."
  exit 1
fi
echo "task: $task"
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$task"   # up to 10 minutes

read -r exit_code stop_reason < <(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$task" \
  --query 'tasks[0].[containers[0].exitCode, stoppedReason]' --output text)
task_id="${task##*/}"
echo "--- migration output ---"
aws logs get-log-events --log-group-name "/ecs/$CLUSTER" \
  --log-stream-name "migrate/migrate/$task_id" --query 'events[].message' --output text \
  | tr '\t' '\n' || echo "(no logs: the container may not have started)"
echo "::endgroup::"
if [[ "$exit_code" != "0" ]]; then
  echo "::error::Migration failed (exit code: $exit_code; $stop_reason). The app was NOT updated."
  exit 1
fi

echo "::group::Roll the service"
aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" \
  --task-definition "$app_td" --desired-count "$DESIRED_COUNT" \
  --query 'service.deployments[0].id' --output text
aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE"   # up to 10 minutes
echo "::endgroup::"

# "Stable" also describes a service that gave up on the new version and rolled
# back (deployment circuit breaker). Check which version is actually running.
# shellcheck disable=SC2016  # backticks are JMESPath literals, not shell
running="$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
  --query 'services[0].deployments[?status==`PRIMARY`].taskDefinition | [0]' --output text)"
if [[ "$running" != "$app_td" ]]; then
  echo "::error::The new version failed its health checks and was rolled back. Running: $running"
  exit 1
fi
echo "Deployed $IMAGE"
