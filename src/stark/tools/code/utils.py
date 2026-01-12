import difflib
from pathlib import Path

def get_full_path(path: str, workspace_dir: Path = None) -> Path:
    """Convert relative path to absolute path within workspace."""
    full_path = Path(path)
    if not full_path.is_absolute():
        full_path = workspace_dir / path
    return full_path.resolve()

def request_approval(operation: str, details: str) -> bool:
        """
        Request user approval for an operation.
        
        Args:
            operation: Type of operation (e.g., "write", "delete")
            details: Details about the operation
            
        Returns:
            True if approved, False otherwise
        """
        print(f"\n{'='*60}")
        print(f"APPROVAL REQUIRED: {operation}")
        print(f"{'='*60}")
        print(details)
        print(f"{'='*60}")
        
        while True:
            response = input("\nApprove this operation? (yes/no/details): ").strip().lower()
            if response in ['yes', 'y']:
                return True
            elif response in ['no', 'n']:
                return False
            elif response in ['details', 'd']:
                print(f"\nOperation Details:\n{details}")
            else:
                print("Invalid response. Please enter 'yes', 'no', or 'details'.")

def generate_diff(original: str, modified: str, filepath: str = "file") -> str:
        """
        Generate unified diff between original and modified content.
        
        Args:
            original: Original content
            modified: Modified content
            filepath: File path for diff header
            
        Returns:
            Unified diff string
        """
        original_lines = original.splitlines(keepends=True)
        modified_lines = modified.splitlines(keepends=True)
        
        diff = difflib.unified_diff(
            original_lines,
            modified_lines,
            fromfile=f"a/{filepath}",
            tofile=f"b/{filepath}",
            lineterm=''
        )
        
        return ''.join(diff)