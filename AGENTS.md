# Repository operating instructions

## Deployment

Pushing code does not update the running bot process.

After pushing any change that affects runtime code, dependencies, configuration, or the
database schema:

1. Restart the service with `sudo -n systemctl restart sloperator`.
2. Confirm `sloperator.service` is active and has a new `MainPID` and start timestamp.
3. Check the startup journal and verify that Slack Socket Mode established a new session.
4. Report the deployment result, not only the Git push result.

Documentation-only and test-only changes do not require a service restart.

## Automated agent prompts

Every new or updated cron or Slack trigger that invokes an agent must include
`AUTOMATED_RESPONSE_STYLE` from `sloperator.automated_session_policy` in its initial prompt.
The first Slack-facing response must be short, direct, understandable to any team member, and
free of filler or unnecessary technical detail unless the user explicitly requests a deeper
explanation. Incident responses must lead with what happened and why (or clearly say the cause is
not established), then give concrete impact, confidence, and the next action. Detailed evidence
belongs in an attachment or a later explicitly requested reply.
