package finops.valid_instance_type

import rego.v1

# Allowed EC2 instance types in this environment.
# Any instance type the LLM outputs must be in this list.
allowed_types := {
    "t3.micro",
    "t3.small",
    "t3.medium",
    "t3.large",
    "t3.xlarge",
    "m5.large",
    "m5.xlarge",
    "c5.large",
    "c5.xlarge",
    "c5.2xlarge",
    "r5.large",
    "r5.xlarge",
}

# Collect every aws_instance resource from the plan
instances[name] = config if {
    resource := input.resource_changes[_]
    resource.type == "aws_instance"
    resource.change.actions[_] != "delete"
    name   := resource.name
    config := resource.change.after
}

# Deny if any instance uses a type not in the allowed list
deny contains msg if {
    some name, config in instances
    not config.instance_type in allowed_types
    msg := sprintf(
        "aws_instance.%s uses disallowed instance_type '%s' — must be one of %v",
        [name, config.instance_type, allowed_types],
    )
}
