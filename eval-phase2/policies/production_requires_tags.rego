package finops.production_requires_tags

import rego.v1

# Every managed resource must have Name and Environment tags.
# Without these, cost attribution and blast-radius detection break.

required_tags := {"Name", "Environment"}

managed_resource_types := {
    "aws_instance",
    "aws_ebs_volume",
    "aws_s3_bucket",
    "aws_cloudwatch_log_group",
}

# Deny any managed resource missing a required tag
deny contains msg if {
    resource := input.resource_changes[_]
    resource.type in managed_resource_types
    resource.change.actions[_] != "delete"

    config := resource.change.after
    tag    := required_tags[_]

    not config.tags[tag]

    msg := sprintf(
        "%s.%s is missing required tag '%s'",
        [resource.type, resource.name, tag],
    )
}
