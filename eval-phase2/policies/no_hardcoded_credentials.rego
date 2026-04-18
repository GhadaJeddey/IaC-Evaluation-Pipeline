package finops.no_hardcoded_credentials

# Detect AWS access keys, secret patterns, and passwords hardcoded in
# resource attributes (user_data, tags, environment variables, etc.).
# LLMs sometimes embed fake-looking but pattern-valid credentials.

import rego.v1

# AWS access key ID pattern: AKIA + 16 uppercase alphanumeric chars
aws_key_pattern := `AKIA[0-9A-Z]{16}`

# Suspicious attribute names that commonly carry secrets
sensitive_keys := {
    "password", "secret", "token", "api_key", "private_key",
    "secret_key", "access_key", "credential", "auth",
}

# Flatten all string leaf values from a resource's after config
string_values(obj) := vals if {
    vals := {v | walk(obj, [_, v]); is_string(v)}
}

# Check all non-delete resources for AWS key patterns in string values
deny contains msg if {
    resource := input.resource_changes[_]
    resource.change.actions[_] != "delete"

    some val in string_values(resource.change.after)
    regex.match(aws_key_pattern, val)

    msg := sprintf(
        "%s.%s contains a string matching an AWS access key pattern — remove hardcoded credentials",
        [resource.type, resource.name],
    )
}

# Check for sensitive attribute names with non-empty values
deny contains msg if {
    resource := input.resource_changes[_]
    resource.change.actions[_] != "delete"

    some key in sensitive_keys
    val := object.get(resource.change.after, key, null)
    val != null
    val != ""
    is_string(val)

    msg := sprintf(
        "%s.%s has a non-empty '%s' attribute — avoid hardcoding secrets in Terraform",
        [resource.type, resource.name, key],
    )
}
