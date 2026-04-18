package finops.valid_volume_type

import rego.v1

# AWS only accepts specific volume type strings.
# LLMs fabricate values like "ssd", "standard-encrypted", "gp4".

valid_types := {"gp2", "gp3", "io1", "io2", "st1", "sc1", "standard"}

# Check root_block_device on aws_instance
deny contains msg if {
    resource := input.resource_changes[_]
    resource.type == "aws_instance"
    resource.change.actions[_] != "delete"

    disk := resource.change.after.root_block_device[_]
    disk.volume_type
    not disk.volume_type in valid_types

    msg := sprintf(
        "aws_instance.%s root_block_device has invalid volume_type '%s' — must be one of %v",
        [resource.name, disk.volume_type, valid_types],
    )
}

# Check ebs_block_device on aws_instance
deny contains msg if {
    resource := input.resource_changes[_]
    resource.type == "aws_instance"
    resource.change.actions[_] != "delete"

    disk := resource.change.after.ebs_block_device[_]
    disk.volume_type
    not disk.volume_type in valid_types

    msg := sprintf(
        "aws_instance.%s ebs_block_device has invalid volume_type '%s' — must be one of %v",
        [resource.name, disk.volume_type, valid_types],
    )
}

# Check standalone aws_ebs_volume
deny contains msg if {
    resource := input.resource_changes[_]
    resource.type == "aws_ebs_volume"
    resource.change.actions[_] != "delete"

    vol_type := resource.change.after.type
    vol_type
    not vol_type in valid_types

    msg := sprintf(
        "aws_ebs_volume.%s has invalid type '%s' — must be one of %v",
        [resource.name, vol_type, valid_types],
    )
}
