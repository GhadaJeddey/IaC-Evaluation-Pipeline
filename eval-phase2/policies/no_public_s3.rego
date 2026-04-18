package finops.no_public_s3

import rego.v1

# S3 buckets used for archival (tier-c) must never be publicly accessible.
# Catches accidental ACL settings and missing public access blocks.

public_acls := {"public-read", "public-read-write", "authenticated-read"}

s3_buckets[name] = config if {
    resource := input.resource_changes[_]
    resource.type == "aws_s3_bucket"
    resource.change.actions[_] != "delete"
    name   := resource.name
    config := resource.change.after
}

# Deny explicit public ACL on the bucket resource
deny contains msg if {
    some name, config in s3_buckets
    config.acl in public_acls
    msg := sprintf(
        "aws_s3_bucket.%s has a public ACL '%s' — archival buckets must be private",
        [name, config.acl],
    )
}

# Deny aws_s3_bucket_public_access_block that re-enables public access
deny contains msg if {
    resource := input.resource_changes[_]
    resource.type == "aws_s3_bucket_public_access_block"
    resource.change.actions[_] != "delete"
    config := resource.change.after

    # Any of these set to false means public access is possible
    config.block_public_acls != true
    msg := sprintf(
        "aws_s3_bucket_public_access_block.%s does not block public ACLs — set block_public_acls = true",
        [resource.name],
    )
}
