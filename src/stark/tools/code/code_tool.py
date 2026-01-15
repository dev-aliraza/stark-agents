import subprocess, shutil
from pathlib import Path
from typing import Optional, List, Dict, Any
from ...stark_tool import stark_tool
from .utils import generate_diff, get_full_path

class CodeTool:
    """
    Comprehensive coding tools for AI agents with diff mechanisms and user approval flows.
    Provides file/folder operations, shell execution, and content management with safety checks.
    """

    def __init__(self, workspace_dir: Optional[str] = None):
        """
        Initialize Coding tools.
        
        Args:
            workspace_dir: Base directory for file operations (defaults to current directory)
        """
        self.workspace_dir = Path(workspace_dir) if workspace_dir else Path.cwd()
        self.operation_history: List[Dict[str, Any]] = []

    def __log_operation(self, operation: str, path: str, status: str, details: Optional[str] = None):
        """Log operation to history."""
        self.operation_history.append({
            "operation": operation,
            "path": path,
            "status": status,
            "details": details
        })

    @stark_tool
    def shell_exec(self, cmd: str, dir_path: Optional[str] = None, timeout: int = 30) -> str:
        """
        Execute a shell command. Use to to execute any shell command.
        
        Args:
            cmd: Shell command to execute (string format)
            dir_path: Directory to execute command in (optional, defaults to workspace)
            timeout: Command timeout in seconds (default: 30)
            
        Returns:
            Command output (stdout + stderr) or error message
        """
        exec_dir = get_full_path(dir_path, self.workspace_dir) if dir_path else self.workspace_dir
        
        try:
            # Execute command
            result = subprocess.run(
                cmd,
                input='\n',
                shell=True,
                cwd=str(exec_dir),
                capture_output=True,
                text=True,
                timeout=timeout
            )
            
            output = f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}\n\nReturn Code: {result.returncode}"
            
            self.__log_operation("shell_exec", str(exec_dir), "success", cmd)
            return output
            
        except subprocess.TimeoutExpired:
            error_msg = f"Command timed out after {timeout} seconds"
            self.__log_operation("shell_exec", str(exec_dir), "timeout", cmd)
            return error_msg
            
        except Exception as e:
            error_msg = f"Error executing command: {str(e)}"
            self.__log_operation("shell_exec", str(exec_dir), "error", error_msg)
            return error_msg

    @stark_tool
    def write(self, path: str, content: str, create_dirs: bool = True) -> str:
        """
        Write content to a file.
        
        Args:
            path: File path (relative to workspace or absolute)
            content: Content to write to file (required)
            create_dirs: Create parent directories if they don't exist (default: True)
            
        Returns:
            Success message or error
        """
        full_path = get_full_path(path, self.workspace_dir)
        file_exists = full_path.exists()

        try:
            # Create parent directories if needed
            if create_dirs:
                full_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Write file
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(content)
            
            action = "overwritten" if file_exists else "created"
            self.__log_operation("write", str(full_path), "success", f"File {action}")
            return f"Successfully {action} file: {full_path}"
            
        except Exception as e:
            error_msg = f"Error writing file: {str(e)}"
            self.__log_operation("write", str(full_path), "error", error_msg)
            return error_msg

    @stark_tool
    def read(self, path: str, encoding: str = 'utf-8') -> str:
        """
        Read content from a file.
        
        Args:
            path: File path (relative to workspace or absolute)
            encoding: File encoding (default: utf-8)
            
        Returns:
            File content or error message
        """
        full_path = get_full_path(path, self.workspace_dir)
        
        try:
            if not full_path.exists():
                return f"Error: File does not exist: {full_path}"
            
            if not full_path.is_file():
                return f"Error: Path is not a file: {full_path}"
            
            with open(full_path, 'r', encoding=encoding) as f:
                content = f.read()
            
            self.__log_operation("read", str(full_path), "success")
            return content
            
        except Exception as e:
            error_msg = f"Error reading file: {str(e)}"
            self.__log_operation("read", str(full_path), "error", error_msg)
            return error_msg

    @stark_tool
    def delete(self, path: str, recursive: bool = False) -> str:
        """
        Delete a file or directory with user approval.
        
        Args:
            path: Path to delete (relative to workspace or absolute)
            recursive: If True, delete directories recursively (default: False)
            
        Returns:
            Success message or error
        """
        full_path = get_full_path(path, self.workspace_dir)
        
        if not full_path.exists():
            return f"Error: Path does not exist: {full_path}"
        
        is_dir = full_path.is_dir()
        item_type = "directory" if is_dir else "file"
        
        try:
            if is_dir:
                if recursive:
                    shutil.rmtree(full_path)
                else:
                    full_path.rmdir()
            else:
                full_path.unlink()
            
            self.__log_operation("delete", str(full_path), "success", f"Deleted {item_type}")
            return f"Successfully deleted {item_type}: {full_path}"
            
        except Exception as e:
            error_msg = f"Error deleting {item_type}: {str(e)}"
            self.__log_operation("delete", str(full_path), "error", error_msg)
            return error_msg

    @stark_tool
    def update(self, path: str, search: str, replace: str, count: int = -1) -> str:
        """
        Update file content by searching and replacing text with diff preview.
        
        Args:
            path: File path (relative to workspace or absolute)
            search: Text to search for
            replace: Text to replace with
            count: Maximum number of replacements (-1 for all, default: -1)
            
        Returns:
            Success message with diff or error
        """
        full_path = get_full_path(path, self.workspace_dir)
        
        try:
            if not full_path.exists():
                return f"Error: File does not exist: {full_path}"
            
            # Read original content
            with open(full_path, 'r', encoding='utf-8') as f:
                original_content = f.read()
            
            # Perform replacement
            modified_content = original_content.replace(search, replace, count)
            
            if original_content == modified_content:
                return f"No changes: Search text not found in {full_path}"
            
            # Generate diff
            diff = generate_diff(original_content, modified_content, str(path))
            
            # Count replacements
            num_replacements = original_content.count(search) if count == -1 else min(count, original_content.count(search))
            
            # Write updated content
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(modified_content)
            
            self.__log_operation("update", str(full_path), "success", f"{num_replacements} replacements")
            return f"Successfully updated {full_path}\nReplacements: {num_replacements}\n\nDIFF:\n{diff}"
            
        except Exception as e:
            error_msg = f"Error updating file: {str(e)}"
            self.__log_operation("update", str(full_path), "error", error_msg)
            return error_msg

    @stark_tool
    def create_directory(self, path: str, parents: bool = True) -> str:
        """
        Create a directory with user approval.
        
        Args:
            path: Directory path (relative to workspace or absolute)
            parents: Create parent directories if needed (default: True)
            
        Returns:
            Success message or error
        """
        full_path = get_full_path(path, self.workspace_dir)
        
        if full_path.exists():
            return f"Error: Path already exists: {full_path}"
        
        try:
            full_path.mkdir(parents=parents, exist_ok=False)
            self.__log_operation("create_directory", str(full_path), "success")
            return f"Successfully created directory: {full_path}"
            
        except Exception as e:
            error_msg = f"Error creating directory: {str(e)}"
            self.__log_operation("create_directory", str(full_path), "error", error_msg)
            return error_msg

    @stark_tool
    def list_directory(self, path: str = ".", pattern: str = "*", recursive: bool = False) -> str:
        """
        List files and directories in a path.
        
        Args:
            path: Directory path (relative to workspace or absolute, default: current)
            pattern: Glob pattern to filter results (default: "*")
            recursive: List recursively (default: False)
            
        Returns:
            Formatted list of files/directories or error
        """
        full_path = get_full_path(path, self.workspace_dir)
        
        try:
            if not full_path.exists():
                return f"Error: Path does not exist: {full_path}"
            
            if not full_path.is_dir():
                return f"Error: Path is not a directory: {full_path}"
            
            # Get items
            if recursive:
                items = sorted(full_path.rglob(pattern))
            else:
                items = sorted(full_path.glob(pattern))
            
            # Format output
            result = [f"Directory: {full_path}\n"]
            result.append(f"Pattern: {pattern}")
            result.append(f"Recursive: {recursive}")
            result.append(f"Total items: {len(items)}\n")
            
            for item in items:
                rel_path = item.relative_to(full_path)
                item_type = "DIR " if item.is_dir() else "FILE"
                size = f"{item.stat().st_size:>10}" if item.is_file() else " " * 10
                result.append(f"{item_type} {size} {rel_path}")
            
            self.__log_operation("list_directory", str(full_path), "success")
            return "\n".join(result)
            
        except Exception as e:
            error_msg = f"Error listing directory: {str(e)}"
            self.__log_operation("list_directory", str(full_path), "error", error_msg)
            return error_msg

    @stark_tool
    def move(self, source: str, destination: str) -> str:
        """
        Move or rename a file/directory with user approval.
        
        Args:
            source: Source path (relative to workspace or absolute)
            destination: Destination path (relative to workspace or absolute)
            
        Returns:
            Success message or error
        """
        src_path = get_full_path(source, self.workspace_dir)
        dst_path = get_full_path(destination, self.workspace_dir)
        
        if not src_path.exists():
            return f"Error: Source does not exist: {src_path}"
        
        try:
            shutil.move(str(src_path), str(dst_path))
            self.__log_operation("move", str(src_path), "success", f"Moved to {dst_path}")
            return f"Successfully moved {src_path} to {dst_path}"
            
        except Exception as e:
            error_msg = f"Error moving: {str(e)}"
            self.__log_operation("move", str(src_path), "error", error_msg)
            return error_msg

    @stark_tool
    def copy(self, source: str, destination: str, recursive: bool = True) -> str:
        """
        Copy a file or directory with user approval.
        
        Args:
            source: Source path (relative to workspace or absolute)
            destination: Destination path (relative to workspace or absolute)
            recursive: Copy directories recursively (default: True)
            
        Returns:
            Success message or error
        """
        src_path = get_full_path(source, self.workspace_dir)
        dst_path = get_full_path(destination, self.workspace_dir)
        
        if not src_path.exists():
            return f"Error: Source does not exist: {src_path}"
        
        is_dir = src_path.is_dir()
        
        try:
            if is_dir:
                if recursive:
                    if dst_path.exists():
                        shutil.rmtree(dst_path)
                    shutil.copytree(src_path, dst_path)
                else:
                    return "Error: Source is a directory but recursive=False"
            else:
                shutil.copy2(src_path, dst_path)
            
            self.__log_operation("copy", str(src_path), "success", f"Copied to {dst_path}")
            return f"Successfully copied {src_path} to {dst_path}"
            
        except Exception as e:
            error_msg = f"Error copying: {str(e)}"
            self.__log_operation("copy", str(src_path), "error", error_msg)
            return error_msg

    @stark_tool
    def get_operation_history(self) -> str:
        """
        Get the history of all operations performed.
        
        Returns:
            Formatted operation history
        """
        if not self.operation_history:
            return "No operations recorded yet."
        
        result = ["Operation History:\n"]
        for i, op in enumerate(self.operation_history, 1):
            result.append(f"{i}. {op['operation'].upper()}")
            result.append(f"   Path: {op['path']}")
            result.append(f"   Status: {op['status']}")
            if op['details']:
                result.append(f"   Details: {op['details']}")
            result.append("")
        
        return "\n".join(result)