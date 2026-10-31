# Incident Response Runbook

## Severity levels
SEV1: full outage or confirmed data breach affecting customers. SEV2: major feature degraded for many customers. SEV3: minor degradation with a workaround.

## Paging and roles
For a SEV1, the on-call engineer pages the Incident Commander through PagerDuty within 5 minutes. The Incident Commander assigns a Communications Lead and a Scribe. The current Incident Commander rotation is managed by the SRE team.

## Customer communication
For SEV1 incidents, the first status page update must be published within 15 minutes of declaration, then every 30 minutes until resolution.

## Data breach procedure
If customer data may have been exposed, the Incident Commander must notify the General Counsel immediately. Regulators and affected customers must be notified within 72 hours of confirming a breach, as required under GDPR.

## Rollback
Every production deploy must be reversible. The rollback command is helixctl deploy rollback --to previous, and it must be executed by someone with the prod-deployer role.

## Postmortems
A blameless postmortem is required for every SEV1 and SEV2 within 5 business days. Action items are tracked in Linear with the label postmortem.
