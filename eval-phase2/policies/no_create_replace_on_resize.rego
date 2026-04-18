package finops.no_create_replace_on_resize

import rego.v1

# A DOWNSIZE action must result in an in-place update (actions = ["update"]).
# If Terraform plans ["delete", "create"] or ["create", "delete"] it means
# the instance will be destroyed and recreated — catastrophic for production.

destroy_and_recreate := {["delete", "create"], ["create", "delete"]}

deny contains msg if {
    resource := input.resource_changes[_]
    resource.type == "aws_instance"
    resource.change.actions in destroy_and_recreate

    msg := sprintf(
        "aws_instance.%s is planned for destroy+recreate %v — a resize must be an in-place update only",
        [resource.name, resource.change.actions],
    )
}
