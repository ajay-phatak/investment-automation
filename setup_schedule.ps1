# setup_schedule.ps1
#
# Registers the weekend-spread Thesis Research scheduled tasks in Windows
# Task Scheduler. The weekend worker fires every 3 hours across Sat/Sun (16
# slots), each run researching one pending thesis. The batch auto-assembles
# the moment the last unit finishes; the Monday task is only a fallback.
#
# Run once (re-running is safe — it replaces the existing tasks):
#     powershell -ExecutionPolicy Bypass -File setup_schedule.ps1
#
# Tasks run under your account, only while you're logged on (fine for a
# personal desktop). Missed runs (machine asleep) recover via StartWhenAvailable.

$ErrorActionPreference = 'Stop'

$scriptDir    = $PSScriptRoot
$researchBat  = Join-Path $scriptDir 'run_research_next.bat'
$assembleBat  = Join-Path $scriptDir 'run_assemble.bat'

foreach ($bat in @($researchBat, $assembleBat)) {
    if (-not (Test-Path $bat)) {
        throw "Missing $bat - run this script from the market-research-agent folder."
    }
}

$weekendTask   = 'ThesisResearch-Weekend'
$assembleTask  = 'ThesisResearch-Assemble'
# Legacy: the standalone Friday auth pre-flight is retired now that the agent
# auths via a long-lived setup-token and the assembly task carries the expiry
# countdown. Listed only so re-running this script unregisters any old copy.
$preflightTask = 'ThesisResearch-Preflight'

# Wake the machine from sleep/hibernate for each slot (WakeToRun); recover runs
# missed while fully off (StartWhenAvailable); tolerate battery power; cap runtime.
# NOTE: WakeToRun wakes a SLEEPING or HIBERNATING machine — it cannot power on a
# machine that is fully shut down. It also needs "Allow wake timers" enabled in
# the active power plan (on by default on AC; often off on battery).
$settings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

# Weekend worker: every 3 hours, midnight-to-9PM on BOTH Saturday and Sunday
# (16 slots). A full second day means a weekend where Saturday is entirely
# missed (machine off) still completes on Sunday. Once 6 units finish the batch
# auto-assembles and every later slot no-ops, so surplus slots cost nothing.
$weekendTriggers = @(
    # Saturday — every 3h, 00:00 → 21:00
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At '12:00AM'
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At '3:00AM'
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At '6:00AM'
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At '9:00AM'
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At '12:00PM'
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At '3:00PM'
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At '6:00PM'
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At '9:00PM'
    # Sunday — every 3h, 00:00 → 21:00
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday   -At '12:00AM'
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday   -At '3:00AM'
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday   -At '6:00AM'
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday   -At '9:00AM'
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday   -At '12:00PM'
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday   -At '3:00PM'
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday   -At '6:00PM'
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday   -At '9:00PM'
)
$weekendAction = New-ScheduledTaskAction -Execute $researchBat -WorkingDirectory $scriptDir

# Assembly: Monday 7:00 AM, before the market open.
$assembleTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At '7:00AM'
$assembleAction  = New-ScheduledTaskAction -Execute $assembleBat -WorkingDirectory $scriptDir

# Replace any existing copies so re-running this script is idempotent. The
# retired pre-flight task is included so it gets removed on re-run.
foreach ($name in @($weekendTask, $assembleTask, $preflightTask)) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "Removed existing task: $name"
    }
}

Register-ScheduledTask -TaskName $weekendTask `
    -Description 'Thesis Research - research one pending thesis per slot, spread across the weekend.' `
    -Trigger $weekendTriggers -Action $weekendAction -Settings $settings | Out-Null
Write-Host "Registered: $weekendTask  (16 weekend slots, every 3h Sat/Sun)"

Register-ScheduledTask -TaskName $assembleTask `
    -Description 'Thesis Research - assemble the weekend batch into the Monday report.' `
    -Trigger $assembleTrigger -Action $assembleAction -Settings $settings | Out-Null
Write-Host "Registered: $assembleTask  (Monday 7:00 AM)"

Write-Host ''
Write-Host 'Done. Verify with:'
Write-Host "    Get-ScheduledTask -TaskName 'ThesisResearch-*'"
Write-Host 'Remove later with:'
Write-Host "    Unregister-ScheduledTask -TaskName 'ThesisResearch-Weekend','ThesisResearch-Assemble','ThesisResearch-Preflight' -Confirm:`$false"
