"""Provider wire schemas are separate copies of local business contracts."""

from copy import deepcopy


def provider_schema(schema: dict) -> dict:
    normalized = deepcopy(schema)

    def visit(node):
        if not isinstance(node, dict):
            return
        node.pop("default", None)
        node_type = node.get("type")
        if node_type == "object" or "properties" in node or (
            isinstance(node_type, list) and "object" in node_type
        ):
            node["required"] = list(node.get("properties", {}))
            node["additionalProperties"] = False
        # These dictionaries map names to schemas; "default" can be a property name.
        for keyword in ("properties", "$defs", "definitions", "patternProperties", "dependentSchemas"):
            for child in node.get(keyword, {}).values():
                visit(child)
        for keyword in ("items", "additionalProperties", "contains", "not", "if", "then", "else", "propertyNames"):
            visit(node.get(keyword))
        for keyword in ("anyOf", "oneOf", "allOf", "prefixItems"):
            for child in node.get(keyword, []):
                visit(child)

    visit(normalized)
    return normalized
