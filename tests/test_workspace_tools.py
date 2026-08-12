import pytest

from stark.tools import WorkspaceTools

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def workspace(tmp_path):
    (tmp_path / "find_research.py").write_text(
        "import sys\nprint('found:', ' '.join(sys.argv[1:]))\n", encoding="utf-8"
    )
    (tmp_path / "notes.md").write_text("# notes\nsome text\n", encoding="utf-8")
    return WorkspaceTools(tmp_path)


async def test_list_shows_files(workspace):
    output = await workspace.call("workspace_list", {})
    assert "find_research.py" in output
    assert "notes.md" in output


async def test_list_honours_pattern(workspace):
    output = await workspace.call("workspace_list", {"pattern": "*.md"})
    assert "notes.md" in output
    assert "find_research.py" not in output


async def test_read_returns_contents(workspace):
    assert "some text" in await workspace.call("workspace_read", {"path": "notes.md"})


async def test_read_rejects_paths_outside_the_agent_dir(workspace):
    output = await workspace.call("workspace_read", {"path": "../../etc/passwd"})
    assert "outside your agent directory" in output


async def test_run_executes_python_script_with_args(workspace):
    output = await workspace.call(
        "workspace_run", {"script": "find_research.py", "args": ["checkout", "latency"]}
    )
    assert "exit code: 0" in output
    assert "found: checkout latency" in output


async def test_run_rejects_script_outside_the_agent_dir(workspace):
    output = await workspace.call("workspace_run", {"script": "/bin/ls"})
    assert "outside your agent directory" in output


async def test_run_reports_missing_script(workspace):
    assert "no such script" in await workspace.call("workspace_run", {"script": "ghost.py"})


async def test_run_rejects_non_executable_non_python(workspace):
    output = await workspace.call("workspace_run", {"script": "notes.md"})
    assert "neither a .py file nor executable" in output


async def test_run_times_out(tmp_path):
    (tmp_path / "hang.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    output = await WorkspaceTools(tmp_path).call(
        "workspace_run", {"script": "hang.py", "timeout": 1}
    )
    assert "timed out after 1s" in output


async def test_unknown_tool(workspace):
    assert "unknown workspace tool" in await workspace.call("workspace_nope", {})
