from ion.instructions import InstructionResolver


def test_agents_wins_over_claude_at_same_scope_and_nested_rules_append(tmp_path):
    (tmp_path / "AGENTS.md").write_text("root agents")
    (tmp_path / "CLAUDE.md").write_text("root claude")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "CLAUDE.md").write_text("src claude")
    sources = InstructionResolver().resolve(tmp_path, [tmp_path / "src" / "app.py"])
    assert [(item.path, item.text) for item in sources] == [
        ("AGENTS.md", "root agents"),
        ("src/CLAUDE.md", "src claude"),
    ]


def test_instruction_symlinks_are_not_loaded(tmp_path):
    target = tmp_path / "secret.md"
    target.write_text("do not load")
    (tmp_path / "AGENTS.md").symlink_to(target)
    assert InstructionResolver().resolve(tmp_path, []) == []
