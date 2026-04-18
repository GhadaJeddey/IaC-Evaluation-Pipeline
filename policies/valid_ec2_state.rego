package finops.valid_ec2_state

import rego.v1

# aws_ec2_instance_state.state only accepts "stopped", "running", or "terminated".
# LLMs write "halt", "off", "paused", "shutdown" — all invalid.

valid_states := {"stopped", "running", "terminated"}

deny contains msg if {
    resource := input.resource_changes[_]
    resource.type == "aws_ec2_instance_state"
    resource.change.actions[_] != "delete"

    state := resource.change.after.state
    not state in valid_states

    msg := sprintf(
        "aws_ec2_instance_state.%s has invalid state '%s' — must be one of %v",
        [resource.name, state, valid_states],
    )
}
