package finops.no_ami_change

import rego.v1

# The AMI must never change during a FinOps action.
# LLMs frequently swap AMIs when generating a resize or stop action —
# this would force an instance replacement and cause downtime.

deny contains msg if {
    resource := input.resource_changes[_]
    resource.type == "aws_instance"
    resource.change.actions[_] == "update"

    before_ami := resource.change.before.ami
    after_ami  := resource.change.after.ami
    before_ami != after_ami

    msg := sprintf(
        "aws_instance.%s changes AMI from '%s' to '%s' — AMI must not change during a FinOps action",
        [resource.name, before_ami, after_ami],
    )
}
