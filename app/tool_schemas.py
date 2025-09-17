# app/tool_schemas.py

TOOL_SCHEMAS = {
    "listdir": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The path to the directory to list."},
        },
        "required": [],
    },
    "read_file": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The path to the file to read."},
        },
        "required": ["path"],
    },
    "write_file": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The path to the file to write."},
            "content": {"type": "string", "description": "The content to write to the file."},
            "overwrite": {"type": "boolean", "description": "Whether to overwrite the file if it already exists.", "default": True},
        },
        "required": ["path", "content"],
    },
    "delete": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The path to the file or directory to delete."},
        },
        "required": ["path"],
    },
}
