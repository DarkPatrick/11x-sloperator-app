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
