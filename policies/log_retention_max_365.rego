package finops.log_retention_max_365

import rego.v1

# Retention beyond 365 days is rarely justified for application logs
# and significantly increases CloudWatch storage costs.
# FinOps policy: cap at 365 days for all log groups.

log_groups[name] = config if {
    resource := input.resource_changes[_]
    resource.type == "aws_cloudwatch_log_group"
    resource.change.actions[_] != "delete"
    name   := resource.name
    config := resource.change.after
}

deny contains msg if {
    some name, config in log_groups
    config.retention_in_days > 365
    msg := sprintf(
        "aws_cloudwatch_log_group.%s retention_in_days=%d exceeds the 365-day FinOps cap",
        [name, config.retention_in_days],
    )
}
