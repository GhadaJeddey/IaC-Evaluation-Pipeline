package finops.no_unencrypted_volumes

import rego.v1

# Every root_block_device and ebs_block_device on a production EC2 instance
# must have encrypted = true.
# All current scenarios have encrypted = false — this rule catches that.

instances[name] = config if {
    resource := input.resource_changes[_]
    resource.type == "aws_instance"
    resource.change.actions[_] != "delete"
    name   := resource.name
    config := resource.change.after
}

# Deny unencrypted root block device
deny contains msg if {
    some name, config in instances
    disk := config.root_block_device[_]
    not disk.encrypted
    msg := sprintf(
        "aws_instance.%s has an unencrypted root_block_device — set encrypted = true",
        [name],
    )
}

# Deny unencrypted additional EBS volumes
deny contains msg if {
    some name, config in instances
    disk := config.ebs_block_device[_]
    not disk.encrypted
    msg := sprintf(
        "aws_instance.%s has an unencrypted ebs_block_device — set encrypted = true",
        [name],
    )
}

# Deny standalone aws_ebs_volume resources without encryption
deny contains msg if {
    resource := input.resource_changes[_]
    resource.type == "aws_ebs_volume"
    resource.change.actions[_] != "delete"
    not resource.change.after.encrypted
    msg := sprintf(
        "aws_ebs_volume.%s is not encrypted — set encrypted = true",
        [resource.name],
    )
}
