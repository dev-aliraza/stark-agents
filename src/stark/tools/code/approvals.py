from .utils import request_approval, get_full_path, generate_diff
from pathlib import Path

def __get_dir(dir):
    return Path(dir) if dir else Path.cwd()

def approval_shell_exec(tool_name: str, arguments: dict, workspace_dir=None):
    # Request approval
    workspace_dir = __get_dir(workspace_dir)
    cmd: str = arguments.get("cmd", "")
    dir_path: str = arguments.get("dir_path", ""),
    timeout: int = arguments.get("timeout", 30)
    exec_dir = get_full_path(dir_path, workspace_dir) if dir_path else workspace_dir

    approval_details = f"""
Command: {cmd}
Working Directory: {exec_dir}
Timeout: {timeout}s
"""
    return request_approval(f"SHELL EXECUTION ({tool_name})", approval_details)


def approval_write(tool_name: str, arguments: dict, workspace_dir=None):
    workspace_dir = __get_dir(workspace_dir)
    path: str = arguments.get("path", "")
    content: str = arguments.get("content", ""),

    full_path = get_full_path(path, workspace_dir)
    file_exists = full_path.exists()

    # Prepare approval details
    if file_exists:
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                original_content = f.read()
            diff = generate_diff(original_content, content, str(path))
            approval_details = f"""
File: {full_path}
Action: OVERWRITE existing file
Size: {len(content)} bytes

DIFF:
{diff}
"""
        except Exception as e:
            approval_details = f"""
File: {full_path}
Action: OVERWRITE existing file (could not read original: {str(e)})
Size: {len(content)} bytes
"""
    else:
        approval_details = f"""
File: {full_path}
Action: CREATE new file
Size: {len(content)} bytes

CONTENT PREVIEW (first 500 chars):
{content[:500]}
{'...' if len(content) > 500 else ''}
"""
    return request_approval(f"WRITE FILE ({tool_name})", approval_details)


def approval_delete(tool_name: str, arguments: dict, workspace_dir=None):
    workspace_dir = __get_dir(workspace_dir)
    path: str = arguments.get("path", "")
    recursive: str = arguments.get("recursive", False)

    full_path = get_full_path(path, workspace_dir)
    is_dir = full_path.is_dir()
    item_type = "directory" if is_dir else "file"

    # Prepare approval details
    if is_dir:
        try:
            items = list(full_path.rglob('*')) if recursive else list(full_path.iterdir())
            approval_details = f"""
Path: {full_path}
Type: Directory
Recursive: {recursive}
Items to delete: {len(items)}

WARNING: This will permanently delete the directory and its contents!
"""
        except Exception as e:
            approval_details = f"""
Path: {full_path}
Type: Directory
Error listing contents: {str(e)}
"""
    else:
        approval_details = f"""
Path: {full_path}
Type: File
Size: {full_path.stat().st_size} bytes

WARNING: This will permanently delete the file!
"""
    return request_approval(f"DELETE {item_type.upper()} ({tool_name})", approval_details)


def approval_update(tool_name: str, arguments: dict, workspace_dir=None):
    workspace_dir = __get_dir(workspace_dir)
    path: str = arguments.get("path", "")
    search: str = arguments.get("search" "")
    replace: str = arguments.get("replace", "")
    count: str = arguments.get("count", -1)
    full_path = get_full_path(path, workspace_dir)
    with open(full_path, 'r', encoding='utf-8') as f:
        original_content = f.read()
    modified_content = original_content.replace(search, replace, count)
    diff = generate_diff(original_content, modified_content, str(path))
    num_replacements = original_content.count(search) if count == -1 else min(count, original_content.count(search))

    approval_details = f"""
File: {full_path}
Search: {repr(search)}
Replace: {repr(replace)}
Replacements: {num_replacements}

DIFF:
{diff}
"""
    return request_approval(f"UPDATE FILE ({tool_name})", approval_details)


def approval_create_dir(tool_name: str, arguments: dict, workspace_dir=None):
    workspace_dir = __get_dir(workspace_dir)
    path: str = arguments.get("path", "")
    parents: str = arguments.get("parents", True)
    full_path = get_full_path(path, workspace_dir)
    approval_details = f"""
Directory: {full_path}
Create Parents: {parents}
"""
    return request_approval(f"CREATE DIRECTORY ({tool_name})", approval_details)


def approval_move(tool_name: str, arguments: dict, workspace_dir=None):
    workspace_dir = __get_dir(workspace_dir)
    source: str = arguments.get("source", "")
    destination: str = arguments.get("destination", "")

    src_path = get_full_path(source, workspace_dir)
    dst_path = get_full_path(destination, workspace_dir)
        
    if not src_path.exists():
        return f"Error: Source does not exist: {src_path}"

    approval_details = f"""
Source: {src_path}
Destination: {dst_path}
Type: {"Directory" if src_path.is_dir() else "File"}
"""
        
    if dst_path.exists():
        approval_details += f"\nWARNING: Destination already exists and will be overwritten!"

    return request_approval(f"MOVE/RENAME ({tool_name})", approval_details)


def approval_copy(tool_name: str, arguments: dict, workspace_dir=None):
    workspace_dir = __get_dir(workspace_dir)
    source: str = arguments.get("source", "")
    destination: str = arguments.get("destination", "")
    recursive: str = arguments.get("recursive", True)

    src_path = get_full_path(source, workspace_dir)
    dst_path = get_full_path(destination, workspace_dir)
    is_dir = src_path.is_dir()

    approval_details = f"""
Source: {src_path}
Destination: {dst_path}
Type: {"Directory" if is_dir else "File"}
Recursive: {recursive if is_dir else "N/A"}
"""
        
    if dst_path.exists():
        approval_details += f"\nWARNING: Destination already exists and will be overwritten!"

    return request_approval(f"COPY ({tool_name})", approval_details)
