package finops.log_retention_required

import rego.v1

# CloudWatch log groups must have an explicit retention policy.
# Tier-c scenario C1 is flagged exactly because retention_in_days is absent,
# causing indefinite (and costly) log storage.

# Valid retention values accepted by AWS (days)
valid_retention_days := {
    1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365,
    400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653,
}

log_groups[name] = config if {
    resource := input.resource_changes[_]
    resource.type == "aws_cloudwatch_log_group"
    resource.change.actions[_] != "delete"
    name   := resource.name
    config := resource.change.after
}

# Deny if retention_in_days is not set (null or 0 means infinite retention)
deny contains msg if {
    some name, config in log_groups
    not config.retention_in_days
    msg := sprintf(
        "aws_cloudwatch_log_group.%s has no retention_in_days — logs will be kept forever (costly)",
        [name],
    )
}

# Deny if the value is not in the AWS-accepted set
deny contains msg if {
    some name, config in log_groups
    config.retention_in_days
    not config.retention_in_days in valid_retention_days
    msg := sprintf(
        "aws_cloudwatch_log_group.%s has invalid retention_in_days=%d — must be one of %v",
        [name, config.retention_in_days, valid_retention_days],
    )
}
