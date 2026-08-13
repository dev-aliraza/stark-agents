import pytest

from stark.tools import FileTools

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def files(tmp_path):
    (tmp_path / "find_research.py").write_text(
        "import sys\nprint('found:', ' '.join(sys.argv[1:]))\n", encoding="utf-8"
    )
    (tmp_path / "notes.md").write_text("# notes\nsome text\n", encoding="utf-8")
    return FileTools(tmp_path)


async def test_list_shows_files(files):
    output = await files.call("file_list", {})
    assert "find_research.py" in output
    assert "notes.md" in output


async def test_list_honours_pattern(files):
    output = await files.call("file_list", {"pattern": "*.md"})
    assert "notes.md" in output
    assert "find_research.py" not in output


async def test_read_returns_contents(files):
    assert "some text" in await files.call("file_read", {"path": "notes.md"})


async def test_read_rejects_paths_outside_the_agent_dir(files):
    output = await files.call("file_read", {"path": "../../etc/passwd"})
    assert "outside your agent directory" in output


async def test_run_executes_python_script_with_args(files):
    output = await files.call(
        "file_run", {"script": "find_research.py", "args": ["checkout", "latency"]}
    )
    assert "exit code: 0" in output
    assert "found: checkout latency" in output


async def test_run_rejects_script_outside_the_agent_dir(files):
    output = await files.call("file_run", {"script": "/bin/ls"})
    assert "outside your agent directory" in output


async def test_run_reports_missing_script(files):
    assert "no such script" in await files.call("file_run", {"script": "ghost.py"})


async def test_run_rejects_non_executable_non_python(files):
    output = await files.call("file_run", {"script": "notes.md"})
    assert "neither a .py file nor executable" in output


async def test_run_times_out(tmp_path):
    (tmp_path / "hang.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    output = await FileTools(tmp_path).call(
        "file_run", {"script": "hang.py", "timeout": 1}
    )
    assert "timed out after 1s" in output


async def test_unknown_tool(files):
    assert "unknown file tool" in await files.call("file_nope", {})


# --- write ----------------------------------------------------------------------------


async def test_write_creates_a_file(files):
    output = await files.call(
        "file_write", {"path": "report.md", "content": "# Report\nAll good.\n"}
    )

    assert "Created report.md" in output
    assert (files.root / "report.md").read_text() == "# Report\nAll good.\n"


async def test_write_reports_the_size_so_the_model_can_check_itself(files):
    output = await files.call("file_write", {"path": "a.txt", "content": "x\ny\nz"})
    assert "5 characters" in output
    assert "3 line(s)" in output


async def test_write_refuses_to_replace_without_being_told_to(files):
    """Clobbering a file the model has not read is silent data loss."""
    output = await files.call("file_write", {"path": "notes.md", "content": "new"})

    assert "already exists" in output
    assert "overwrite=true" in output
    # Untouched.
    assert "some text" in (files.root / "notes.md").read_text()


async def test_write_replaces_when_overwrite_is_set(files):
    output = await files.call(
        "file_write", {"path": "notes.md", "content": "replaced", "overwrite": True}
    )

    assert "Replaced notes.md" in output
    assert (files.root / "notes.md").read_text() == "replaced"


async def test_write_creates_missing_parent_folders(files):
    output = await files.call(
        "file_write", {"path": "out/nested/data.json", "content": "{}"}
    )

    assert "Created" in output
    assert (files.root / "out" / "nested" / "data.json").read_text() == "{}"


async def test_write_rejects_paths_outside_the_agent_dir(files):
    output = await files.call(
        "file_write", {"path": "../escaped.txt", "content": "nope"}
    )

    assert "outside your agent directory" in output
    assert not (files.root.parent / "escaped.txt").exists()


async def test_write_refuses_to_touch_agent_md(tmp_path):
    """An AGENT.md is what the agent *is*, so rewriting it is not a task action."""
    (tmp_path / "AGENT.md").write_text("---\nname: x\n---\n", encoding="utf-8")
    output = await FileTools(tmp_path).call(
        "file_write", {"path": "AGENT.md", "content": "hijacked", "overwrite": True}
    )

    assert "defines this agent" in output
    assert "hijacked" not in (tmp_path / "AGENT.md").read_text()


async def test_write_requires_content(files):
    output = await files.call("file_write", {"path": "a.txt"})
    assert "'content' is required" in output
    assert not (files.root / "a.txt").exists()


async def test_write_accepts_a_deliberately_empty_file(files):
    assert "Created" in await files.call(
        "file_write", {"path": "empty.txt", "content": ""}
    )
    assert (files.root / "empty.txt").read_text() == ""


async def test_write_requires_a_path(files):
    assert "'path' is required" in await files.call(
        "file_write", {"content": "x"}
    )


async def test_write_refuses_content_over_the_limit(files):
    from stark.tools.file import MAX_WRITE_CHARS

    output = await files.call(
        "file_write", {"path": "big.txt", "content": "x" * (MAX_WRITE_CHARS + 1)}
    )

    # Refused, not truncated — half a file written as "success" is worse than an error.
    assert "over the" in output
    assert not (files.root / "big.txt").exists()


async def test_write_refuses_a_folder(files):
    (files.root / "sub").mkdir()
    output = await files.call("file_write", {"path": "sub", "content": "x"})
    assert "is a folder" in output


async def test_a_written_script_can_then_be_run(files):
    """The point of write: produce something, then use it."""
    await files.call(
        "file_write",
        {"path": "generated.py", "content": "print('from a generated script')\n"},
    )
    output = await files.call("file_run", {"script": "generated.py"})

    assert "exit code: 0" in output
    assert "from a generated script" in output


# --- delete ---------------------------------------------------------------------------


async def test_delete_removes_a_file(files):
    output = await files.call("file_delete", {"path": "notes.md"})

    assert "Deleted notes.md" in output
    assert not (files.root / "notes.md").exists()


async def test_delete_reports_a_missing_file(files):
    assert "no such file" in await files.call("file_delete", {"path": "ghost.md"})


async def test_delete_rejects_paths_outside_the_agent_dir(tmp_path):
    outside = tmp_path / "keep.txt"
    outside.write_text("important", encoding="utf-8")
    root = tmp_path / "agent"
    root.mkdir()

    output = await FileTools(root).call("file_delete", {"path": "../keep.txt"})

    assert "outside your agent directory" in output
    assert outside.exists()


async def test_delete_refuses_agent_md(tmp_path):
    (tmp_path / "AGENT.md").write_text("---\nname: x\n---\n", encoding="utf-8")
    output = await FileTools(tmp_path).call("file_delete", {"path": "AGENT.md"})

    assert "defines this agent" in output
    assert (tmp_path / "AGENT.md").exists()


async def test_delete_refuses_the_agent_directory(files):
    output = await files.call("file_delete", {"path": "."})

    assert "your agent directory itself" in output
    assert files.root.exists()


async def test_delete_removes_an_empty_folder(files):
    (files.root / "scratch").mkdir()

    output = await files.call("file_delete", {"path": "scratch"})

    assert "Deleted empty folder scratch" in output
    assert not (files.root / "scratch").exists()


async def test_delete_refuses_a_folder_with_anything_in_it(files):
    """No recursive delete: too broad to infer from a task description."""
    (files.root / "data").mkdir()
    (files.root / "data" / "keep.csv").write_text("1,2\n", encoding="utf-8")

    output = await files.call("file_delete", {"path": "data"})

    assert "not empty" in output
    assert (files.root / "data" / "keep.csv").exists()


async def test_delete_requires_a_path(files):
    assert "'path' is required" in await files.call("file_delete", {})


# --- the toolset as a whole -----------------------------------------------------------


async def test_both_new_tools_are_offered_to_the_model():
    from stark.tools.file import BUILTIN_TOOL_NAMES, schemas

    offered = {schema["function"]["name"] for schema in schemas()}
    assert offered == set(BUILTIN_TOOL_NAMES)
    assert {"file_write", "file_delete"} <= offered


async def test_the_write_schema_requires_path_and_content():
    from stark.tools.file import schemas

    write = next(s for s in schemas() if s["function"]["name"] == "file_write")
    assert write["function"]["parameters"]["required"] == ["path", "content"]
    # overwrite is optional, so a plain create needs no extra thought.
    assert "overwrite" in write["function"]["parameters"]["properties"]
