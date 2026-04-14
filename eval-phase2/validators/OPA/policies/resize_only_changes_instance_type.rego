package finops.resize_only_changes_instance_type

import rego.v1

# On a DOWNSIZE/RESIZE action the only attribute that should change on
# aws_instance is instance_type. Changing AMI, volume size, tags, or
# network config means the LLM mutated something it was not asked to touch.

# Attributes allowed to differ between before and after on an update
allowed_to_change := {"instance_type"}

deny contains msg if {
    resource := input.resource_changes[_]
    resource.type == "aws_instance"
    resource.change.actions == ["update"]

    before := resource.change.before
    after  := resource.change.after

    # Check every key present in before that changed
    some key, val_before in before
    val_after := object.get(after, key, null)
    val_before != val_after

    # Ignore keys that are expected to change
    not key in allowed_to_change

    # Ignore internal Terraform meta-keys
    not startswith(key, "timeouts")
    not startswith(key, "id")

    msg := sprintf(
        "aws_instance.%s update changes '%s' which should not be modified during a resize (before=%v, after=%v)",
        [resource.name, key, val_before, val_after],
    )
}
