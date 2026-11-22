#!/usr/bin/env bash
# Deploy one image to an ECS environment (guide Chapter 2):
#
#   1. register new task definitions: the latest ones, with only the image swapped
#   2. run the migration task and wait; stop here if it fails
#   3. roll the web service to the new version and wait until it is stable
#   4. check the new version is really running (not rolled back)
#   5. the same for the worker service (Chapter 6), whose task count is
#      autoscaling's business, not ours
#
# Migrations run BEFORE the new code, so they must be backward compatible:
# the old version keeps serving while they run (expand/contract, guide 2.6).
#
# Usage (CI sets these):
#   IMAGE=<registry>/ledgerline@sha256:<digest> CLUSTER=ledgerline-staging scripts/deploy_ecs.sh
# (A tag such as ledgerline:<sha> works too. CI deploys by digest since
# Chapter 8: the digest is what was scanned and signed.)
set -euo pipefail

: "${IMAGE:?set IMAGE to the full image reference}"
: "${CLUSTER:?set CLUSTER}"
SERVICE="${SERVICE:-$CLUSTER-app}"
APP_FAMILY="${APP_FAMILY:-$CLUSTER-app}"
MIGRATE_FAMILY="${MIGRATE_FAMILY:-$CLUSTER-migrate}"
WORKER_SERVICE="${WORKER_SERVICE:-$CLUSTER-worker}"
WORKER_FAMILY="${WORKER_FAMILY:-$CLUSTER-worker}"
DESIRED_COUNT="${DESIRED_COUNT:-1}"

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

# Copy the latest revision of a family, swap the image, register it.
# Terraform owns everything else in the definition (settings, secrets, sizes).
# Only containers running OUR image are changed; sidecars (the CloudWatch
# agent) keep theirs.
register() {
  local family="$1"
  aws ecs describe-task-definition --task-definition "$family" --query taskDefinition --output json \
    | jq --arg image "$IMAGE" '
        (.containerDefinitions[] | select(.image | test("/ledgerline[:@]"))).image = $image
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
# Environments created before Chapter 6 have no worker yet.
worker_status="$(aws ecs describe-services --cluster "$CLUSTER" --services "$WORKER_SERVICE" \
  --query 'services[0].status' --output text 2>/dev/null || echo NONE)"
if [[ "$worker_status" == "ACTIVE" ]]; then
  worker_td="$(register "$WORKER_FAMILY")"
  echo "worker:  $worker_td"
fi
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

if [[ -n "${worker_td:-}" ]]; then
  echo "::group::Roll the worker service"
  # No --desired-count: autoscaling owns it (often 0 in staging). Old workers
  # get SIGTERM, finish their current job and stop; new ones take over.
  aws ecs update-service --cluster "$CLUSTER" --service "$WORKER_SERVICE" \
    --task-definition "$worker_td" --query 'service.deployments[0].id' --output text
  aws ecs wait services-stable --cluster "$CLUSTER" --services "$WORKER_SERVICE"
  echo "::endgroup::"
  # shellcheck disable=SC2016
  running="$(aws ecs describe-services --cluster "$CLUSTER" --services "$WORKER_SERVICE" \
    --query 'services[0].deployments[?status==`PRIMARY`].taskDefinition | [0]' --output text)"
  if [[ "$running" != "$worker_td" ]]; then
    echo "::error::The new worker version failed and was rolled back. Running: $running"
    exit 1
  fi
fi
echo "Deployed $IMAGE"
