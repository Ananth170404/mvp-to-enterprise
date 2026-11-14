<#
Restore drill helper (guide Chapter 5, part 2). Runs `python -m app.drill <command>`
as a one-off ECS task, waits for it, and prints its output.

The database has no public address, so the drill runs inside the VPC: same
task definition, subnets and security group as the migration task, with the
command (and optionally DB_HOST) overridden.

    .\scripts\drill.ps1 prepare
    .\scripts\drill.ps1 state
    .\scripts\drill.ps1 accident
    .\scripts\drill.ps1 state -DbHost <restored instance endpoint>

Needs: AWS CLI v2, credentials for the account (e.g. $Env:AWS_PROFILE = "ledgerline"),
and an image that contains app/drill.py deployed to the environment.
#>
param(
    [Parameter(Mandatory = $true)][ValidateSet("prepare", "state", "accident")][string]$Command,
    [string]$DbHost = "",
    [string]$Cluster = "ledgerline-staging"
)
$ErrorActionPreference = "Stop"

$service = "$Cluster-app"
$family = "$Cluster-migrate"
$tmp = Join-Path $env:TEMP "ledgerline-drill"
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

# Subnets and security group: read from the service, like CI does (one source of truth).
$network = aws ecs describe-services --cluster $Cluster --services $service `
    --query "services[0].networkConfiguration" --output json
if ($LASTEXITCODE -ne 0) { throw "Could not read the service's network configuration." }

$override = @{ name = "migrate"; command = @("python", "-m", "app.drill", $Command) }
if ($DbHost) { $override.environment = @(@{ name = "DB_HOST"; value = $DbHost }) }
$overrides = @{ containerOverrides = @($override) } | ConvertTo-Json -Depth 5

# Files rather than inline JSON: PowerShell 5 mangles quotes in native-command
# arguments. WriteAllText writes UTF-8 without a BOM, which the AWS CLI requires.
[IO.File]::WriteAllText("$tmp\network.json", ($network -join "`n"))
[IO.File]::WriteAllText("$tmp\overrides.json", $overrides)

$task = aws ecs run-task --cluster $Cluster --task-definition $family --launch-type FARGATE `
    --network-configuration "file://$tmp/network.json" --overrides "file://$tmp/overrides.json" `
    --started-by "restore-drill" --query "tasks[0].taskArn" --output text
if ($LASTEXITCODE -ne 0 -or -not $task -or $task -eq "None") { throw "The drill task could not be started." }
$taskId = $task.Split("/")[-1]
Write-Host "task $taskId started; waiting for it to finish (about a minute)..."

aws ecs wait tasks-stopped --cluster $Cluster --tasks $task
$result = aws ecs describe-tasks --cluster $Cluster --tasks $task `
    --query "tasks[0].[containers[?name=='migrate'].exitCode | [0], stoppedReason]" --output text
Write-Host "exit code, reason: $result"

$log = aws logs get-log-events --log-group-name "/ecs/$Cluster" --log-stream-name "migrate/migrate/$taskId" `
    --start-from-head --query "events[].message" --output json
$lines = ($log -join "`n") | ConvertFrom-Json
foreach ($line in $lines) { Write-Host $line }
