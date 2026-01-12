"""
Example usage of Coding tools for AI agents.

This example demonstrates how to use the Coding class with stark agents
to perform file operations, shell commands, and content management with
user approval flows and diff mechanisms.
"""

import os
from stark import Agent, Runner, Util
from stark.tools import CodeTool
from stark.tools.code import approval_write

def unit_test_agent():
    """Example: Basic file operations with user approval."""
    
    # Initialize Coding tools (auto_approve=False requires user confirmation)
    coding_tools = CodeTool()


    
    # Create an agent with coding tools
    agent = Agent(
        name="Coding-Assistant",
        instructions="""### ROLE
You are an Expert Software Development Engineer in Test (SDET) and Senior Code Reviewer. Your specialty is analyzing source code and generating robust, maintainable, and comprehensive unit tests.

### OBJECTIVE
Your goal is to take code snippets provided by the user and output a complete test suite that covers happy paths, edge cases, error handling, and boundary conditions.

### GUIDELINES & BEST PRACTICES

1.  **Analyze First:** Before writing code, briefly analyze the logic, control flow, and dependencies of the provided snippet.
2.  **Framework detection:** Detect the programming language and use the most standard testing library for that language unless the user specifies otherwise (e.g., Jest/Vitest for JavaScript/TS, Pytest for Python, JUnit for Java, Go testing for Go).
3.  **AAA Pattern:** Structure every test using the **Arrange, Act, Assert** pattern.
4.  **Mocking:** ISOLATE the unit. Do not make real network calls, database queries, or file system changes. Mock all external dependencies, imports, and side effects.
5.  **Coverage:**
    * **Happy Path:** Ensure the standard use case returns expected results.
    * **Edge Cases:** Test boundaries (0, -1, empty strings, nulls, max values).
    * **Error Handling:** Ensure exceptions are raised and handled correctly.
6.  **Naming Conventions:** Use descriptive test names that explain the condition and the expected result (e.g., `it('should return 400 when input is invalid')` or `test_calculate_total_handles_negative_numbers`).

### OUTPUT FORMAT
1.  **Prerequisites:** List necessary libraries to install (if not obvious).
2.  **The Code:** Provide the complete, runnable test file code block.
3.  **Explanation:** (Optional) Briefly explain complex mocking strategies or why specific edge cases were chosen.

### CRITICAL RULES
* If the code is untestable (e.g., extensive side effects without dependency injection), suggest a refactor *before* writing the tests.
* Do not hallucinate methods that do not exist in the source code.
* Keep the tests DRY (Don't Repeat Yourself) by using setup/teardown methods where appropriate.""",
        model="claude-sonnet-4-5-20250929",
        function_tools=[
            coding_tools
        ],
        max_output_tokens=64000
    )
    
    # Example task: Create a Python file
    result = Runner(agent).run(input=[{
        "role": "user",
        "content": "Read `src/stark/mcp.py` and write python unit tests in `tests/` folder."
    }])
    
    print(result)

def example_basic_usage():
    """Example: Basic file operations with user approval."""
    
    # Initialize Coding tools (auto_approve=False requires user confirmation)
    coding_tools = CodeTool(workspace_dir="./workspace")
    
    # Create an agent with coding tools
    agent = Agent(
        name="Coding-Assistant",
        instructions="""You are a coding assistant that helps with file operations.
        Always explain what you're doing before performing operations.
        Use the coding tools to manage files and execute commands safely.""",
        model="claude-sonnet-4-5-20250929",
        function_tools=[
            coding_tools
        ],
        approvals = {
            ".*write.*": Util.pass_function_with_args(approval_write, workspace_dir="./workspace")
        }
    )
    
    # Example task: Create a Python file
    result = Runner(agent).run(input=[{
        "role": "user",
        "content": "Create a new Python file called 'hello.py' with a simple hello world function"
    }])
    
    print(result)


if __name__ == "__main__":
    print("Coding Tools Examples")
    print("=" * 60)
    
    # Uncomment the example you want to run:
    # example_basic_usage()
    # unit_test_agent()