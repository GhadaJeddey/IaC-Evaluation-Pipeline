package finops.no_prevent_destroy_on_terminate

import rego.v1

# When a resource is being deleted (TERMINATE/ZOMBIE action), the LLM
# must not set lifecycle.prevent_destroy = true — this silently blocks
# deletion and Terraform will error instead of removing the resource.

deny contains msg if {
    resource := input.resource_changes[_]
    resource.change.actions[_] == "delete"

    # prevent_destroy is a plan-time constraint surfaced in the plan JSON
    # under resource_changes as a lifecycle block
    resource.change.after_unknown == {}    # resource being removed
    lc := resource.change.before.lifecycle[_]
    lc.prevent_destroy == true

    msg := sprintf(
        "%s.%s is being deleted but has lifecycle.prevent_destroy = true — remove it to allow termination",
        [resource.type, resource.name],
    )
}
