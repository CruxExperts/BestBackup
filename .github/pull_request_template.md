## Summary

<!-- What changes for a user or operator? Identify legacy 1.x or the 2.0.0-alpha.1 preview, if relevant. -->

## Related issue

<!-- Closes #N  or  No related issue -->

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Documentation update
- [ ] Refactor (no behavior change)
- [ ] Other:

## Checklist

- [ ] Code runs without syntax errors (`uv run python -m py_compile bbackup.py bbman.py bbackup/*.py bbackup/data/*.py bbackup/management/*.py scripts/*.py`)
- [ ] Commit messages follow conventional commit format (`feat:`, `fix:`, `docs:`, etc.)
- [ ] Documentation updated if behavior changed
- [ ] No secrets, keys, or personal data included
- [ ] Public Markdown follows the [GitHub Markdown Writing Standard](../docs/standards/github-markdown/github-markdown-writing-standard.md); run `uv run python scripts/check_markdown_standards.py` when docs change.
- [ ] Test and platform claims distinguish local fixtures/Garage evidence from B2, Amazon S3, database, and Ubuntu package qualification.
- [ ] Interrupted mutating operations are not automatically retried; uncertain outcomes are reported for reconciliation.

## Testing

<!-- How did you verify this works? What did you test, and on what OS / Docker version? -->
