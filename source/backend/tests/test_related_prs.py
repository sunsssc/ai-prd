import pytest

from app.business.code_reviews.related_prs import MAX_RELATED_PRS, extract_related_pull_requests


def test_only_explicit_related_section_is_read_and_duplicates_are_collapsed():
    body = '''Reference: https://github.com/outside/reference/pull/1
```markdown
## Related PRs
https://github.com/outside/example/pull/2
```
<!--
## Related PRs
https://github.com/outside/comment/pull/3
-->
## 关联 PR
- [API](https://github.com/Acme/api/pull/42/files#diff-123)
- https://github.com/acme/API/pull/42
- https://github.com/acme/ui/pull/7?diff=split
## Testing
https://github.com/outside/testing/pull/4
'''
    refs = extract_related_pull_requests(body)
    assert [(r.repo_full_name, r.pr_number) for r in refs] == [('Acme/api', 42), ('acme/ui', 7)]


@pytest.mark.parametrize('heading', ['Related PRs', 'Related pull requests', '关联 PR', '关联PR'])
def test_supported_section_headings(heading):
    refs = extract_related_pull_requests(f'## {heading}\n- https://github.com/acme/api/pull/1')
    assert len(refs) == 1


@pytest.mark.parametrize('entry', [
    'http://github.com/acme/api/pull/1',
    'https://github.com.evil.test/acme/api/pull/1',
    'https://github.com@evil.test/acme/api/pull/1',
    'https://github.com/acme/api/issues/1',
    'https://github.com/acme/api/pull/0',
    'acme/api#1',
])
def test_invalid_explicit_association_fails_instead_of_silently_omitting_it(entry):
    with pytest.raises(ValueError, match='关联 PR'):
        extract_related_pull_requests(f'## Related PRs\n- {entry}')


def test_related_scope_is_bounded():
    body = '## Related PRs\n' + '\n'.join(f'- https://github.com/acme/api/pull/{i + 1}' for i in range(MAX_RELATED_PRS + 1))
    with pytest.raises(ValueError, match='不能超过'):
        extract_related_pull_requests(body)


def test_no_association_is_valid():
    assert extract_related_pull_requests('See https://github.com/acme/api/pull/1') == ()
    assert extract_related_pull_requests('## Related PRs\n- None') == ()
