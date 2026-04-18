package finops.no_resource_scope_creep

import rego.v1

# FinOps actions target specific named resources.
# The LLM must not hallucinate extra resource types (IAM roles, security
# groups, VPCs, etc.) alongside the resource it was asked to act on.

allowed_resource_types := {
    "aws_instance",
    "aws_ebs_volume",
    "aws_ec2_instance_state",
    "aws_s3_bucket",
    "aws_s3_bucket_lifecycle_configuration",
    "aws_cloudwatch_log_group",
}

deny contains msg if {
    resource := input.resource_changes[_]
    resource.change.actions[_] != "no-op"
    not resource.type in allowed_resource_types
    msg := sprintf(
        "unexpected resource type '%s' (name: %s) — LLM introduced a resource outside the allowed scope",
        [resource.type, resource.name],
    )
}
