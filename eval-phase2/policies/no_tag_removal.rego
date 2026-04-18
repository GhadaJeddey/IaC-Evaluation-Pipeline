package finops.no_tag_removal

import rego.v1

# On update actions, existing tags must be preserved.
# LLMs sometimes strip tags (Name, Environment) when rewriting a resource block,
# which breaks cost attribution and blast-radius tracking.

deny contains msg if {
    resource := input.resource_changes[_]
    resource.change.actions[_] == "update"

    before_tags := object.get(resource.change.before, "tags", {})
    after_tags  := object.get(resource.change.after,  "tags", {})

    # A tag present before must still be present after
    some key, val in before_tags
    not after_tags[key]

    msg := sprintf(
        "%s.%s removes existing tag '%s'='%s' — all original tags must be preserved",
        [resource.type, resource.name, key, val],
    )
}
