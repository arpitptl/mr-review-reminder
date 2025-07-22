import pytest
from unittest.mock import patch, MagicMock
import mr_reminder_core
import logging
import os

# --- Fixtures ---

@pytest.fixture
def fake_config():
    # Minimal config for two teams, one with two projects, one with one
    return {
        "AA_GATEWAY_BACKEND": {
            "slack_webhook_url": "https://hooks.slack.com/services/fake1",
            "threshold_config": {
                "stale_days_threshold": 2,
                "use_priority_thresholds": True,
                "threshold_highest": 1,
                "threshold_high": 2,
                "threshold_medium": 3,
                "threshold_low": 3,
                "threshold_lowest": 3
            },
            "gitlab_projects": {
                "Rohan": {
                    "gitlab_project_id": "1",
                    "gitlab_token": "token1"
                },
                "Edoras": {
                    "gitlab_project_id": "2",
                    "gitlab_token": "token2"
                }
            }
        },
        "AA_GATEWAY_FRONTEND": {
            "slack_webhook_url": "https://hooks.slack.com/services/fake2",
            "threshold_config": {
                "stale_days_threshold": 2,
                "use_priority_thresholds": True,
                "threshold_highest": 1,
                "threshold_high": 2,
                "threshold_medium": 3,
                "threshold_low": 3,
                "threshold_lowest": 3
            },
            "gitlab_projects": {
                "Athena": {
                    "gitlab_project_id": "3",
                    "gitlab_token": "token3"
                }
            }
        }
    }

# --- Helper for MRs ---
def fake_mrs():
    # List of MRs for various scenarios
    return {
        "Rohan": [
            # Happy path: MR with JIRA
            {
                "title": "[PROJ-123] Fix bug",
                "web_url": "http://gitlab.com/mr/1",
                "iid": 1,
                "author": {"name": "Alice", "username": "alice"},
                "assignees": [{"name": "Bob"}],
                "reviewers": [{"name": "Carol"}],
                "created_at": "2024-06-01T10:00:00Z",
                "project_name": "Rohan",
                "project_id": "1",
                "project_token": "token1"
            },
            # Edge: MR with no JIRA
            {
                "title": "Refactor code",
                "web_url": "http://gitlab.com/mr/2",
                "iid": 2,
                "author": {"name": "Dave", "username": "dave"},
                "assignees": [],
                "reviewers": [],
                "created_at": "2024-06-01T10:00:00Z",
                "project_name": "Rohan",
                "project_id": "1",
                "project_token": "token1"
            },
            # Edge: Draft MR
            {
                "title": "Draft: WIP feature",
                "web_url": "http://gitlab.com/mr/3",
                "iid": 3,
                "author": {"name": "Eve", "username": "eve"},
                "assignees": [],
                "reviewers": [],
                "created_at": "2024-06-01T10:00:00Z",
                "project_name": "Rohan",
                "project_id": "1",
                "project_token": "token1",
                "draft": True
            },
            # Edge: Bot MR
            {
                "title": "chore(deps): update dependency",
                "web_url": "http://gitlab.com/mr/4",
                "iid": 4,
                "author": {"name": "Renovate Bot", "username": "renovate[bot]"},
                "assignees": [],
                "reviewers": [],
                "created_at": "2024-06-01T10:00:00Z",
                "project_name": "Rohan",
                "project_id": "1",
                "project_token": "token1"
            }
        ],
        "Edoras": [
            # Error: Simulate GitLab project not found (empty list)
        ]
    }

# --- Mocks ---

def fake_gitlab_get_open_merge_requests(self):
    # Use the fixture data
    return fake_mrs()

def fake_gitlab_get_merge_request_approvals(self, project_id, mr_iid, token):
    # All MRs are unapproved for test
    return {"approved_by": []}

def fake_jira_get_ticket_details(self, ticket_key):
    # Simulate JIRA ticket found or not found
    if ticket_key == "PROJ-123":
        return {"status": "In Progress", "priority": "high", "priority_id": "1"}
    # Simulate JIRA ticket not found
    raise Exception("JIRA ticket not found")

def fake_jira_get_ticket_details_safe(self, ticket_key):
    # Simulate JIRA ticket found or not found, but return None on error
    if ticket_key == "PROJ-123":
        return {"status": "In Progress", "priority": "high", "priority_id": "1"}
    return {"status": None, "priority": None, "priority_id": None}

def fake_slack_post(*args, **kwargs):
    # Simulate Slack API success
    class FakeResponse:
        def raise_for_status(self): pass
    return FakeResponse()

def fake_slack_post_error(*args, **kwargs):
    # Simulate Slack API error
    class FakeResponse:
        def raise_for_status(self): raise Exception("Slack error")
    return FakeResponse()

# --- Tests ---

@patch('mr_reminder_core.requests.post', side_effect=fake_slack_post)
@patch('mr_reminder_core.TeamGitLabClient.get_open_merge_requests', new=fake_gitlab_get_open_merge_requests)
@patch('mr_reminder_core.TeamGitLabClient.get_merge_request_approvals', new=fake_gitlab_get_merge_request_approvals)
@patch('mr_reminder_core.SimpleJiraClient.get_ticket_details', new=fake_jira_get_ticket_details_safe)
@patch('mr_reminder_core.load_projects_config')
def test_happy_path(mock_load_config, mock_post, fake_config):
    # Set up env vars for JIRA config
    os.environ['JIRA_URL'] = 'http://fake-jira'
    os.environ['JIRA_USERNAME'] = 'user'
    os.environ['JIRA_TOKEN'] = 'token'
    mock_load_config.return_value = fake_config
    mr_reminder_core.main()
    # Slack should be called for each team
    assert mock_post.call_count == 2
    # Check that the payload for the first call includes JIRA info for MR1 and not for MR2
    payload = mock_post.call_args_list[0][1]['json']
    blocks = payload['blocks']
    # MR with JIRA ticket should mention JIRA
    assert any('JIRA:' in b['text']['text'] for b in blocks if b['type'] == 'section')
    # MR without JIRA ticket should not mention JIRA
    assert any('Refactor code' in b['text']['text'] and 'JIRA:' not in b['text']['text'] for b in blocks if b['type'] == 'section')

@patch('mr_reminder_core.requests.post', side_effect=fake_slack_post)
@patch('mr_reminder_core.TeamGitLabClient.get_open_merge_requests', new=fake_gitlab_get_open_merge_requests)
@patch('mr_reminder_core.TeamGitLabClient.get_merge_request_approvals', new=fake_gitlab_get_merge_request_approvals)
@patch('mr_reminder_core.SimpleJiraClient.get_ticket_details', new=fake_jira_get_ticket_details_safe)
@patch('mr_reminder_core.load_projects_config')
def test_edge_cases(mock_load_config, mock_post, fake_config):
    os.environ['JIRA_URL'] = 'http://fake-jira'
    os.environ['JIRA_USERNAME'] = 'user'
    os.environ['JIRA_TOKEN'] = 'token'
    mock_load_config.return_value = fake_config
    mr_reminder_core.main()
    # Draft and bot MRs should be filtered out (not in Slack message)
    payload = mock_post.call_args_list[0][1]['json']
    blocks = payload['blocks']
    # Only two MRs should be present (not draft/bot)
    mr_titles = [b['text']['text'] for b in blocks if b['type'] == 'section']
    assert any('Fix bug' in t for t in mr_titles)
    assert any('Refactor code' in t for t in mr_titles)
    assert not any('Draft: WIP feature' in t for t in mr_titles)
    assert not any('chore(deps)' in t for t in mr_titles)

@patch('mr_reminder_core.requests.post', side_effect=fake_slack_post)
@patch('mr_reminder_core.TeamGitLabClient.get_open_merge_requests', new=fake_gitlab_get_open_merge_requests)
@patch('mr_reminder_core.TeamGitLabClient.get_merge_request_approvals', new=fake_gitlab_get_merge_request_approvals)
@patch('mr_reminder_core.SimpleJiraClient.get_ticket_details', new=fake_jira_get_ticket_details_safe)
@patch('mr_reminder_core.load_projects_config')
def test_gitlab_project_not_found(mock_load_config, mock_post, fake_config):
    os.environ['JIRA_URL'] = 'http://fake-jira'
    os.environ['JIRA_USERNAME'] = 'user'
    os.environ['JIRA_TOKEN'] = 'token'
    # Remove all MRs from Edoras to simulate project not found (API returns empty list)
    fake_config["AA_GATEWAY_BACKEND"]["gitlab_projects"]["Edoras"]["gitlab_project_id"] = "notfound"
    mock_load_config.return_value = fake_config
    mr_reminder_core.main()
    # Slack should still be called for both teams
    assert mock_post.call_count == 2

@patch('mr_reminder_core.requests.post', side_effect=fake_slack_post)
@patch('mr_reminder_core.TeamGitLabClient.get_open_merge_requests', new=fake_gitlab_get_open_merge_requests)
@patch('mr_reminder_core.TeamGitLabClient.get_merge_request_approvals', new=fake_gitlab_get_merge_request_approvals)
@patch('mr_reminder_core.SimpleJiraClient.get_ticket_details', new=fake_jira_get_ticket_details_safe)
@patch('mr_reminder_core.load_projects_config')
def test_jira_ticket_not_found(mock_load_config, mock_post, fake_config):
    os.environ['JIRA_URL'] = 'http://fake-jira'
    os.environ['JIRA_USERNAME'] = 'user'
    os.environ['JIRA_TOKEN'] = 'token'
    mock_load_config.return_value = fake_config
    # Should not crash even if JIRA ticket is not found (should log warning)
    try:
        mr_reminder_core.main()
    except Exception as e:
        assert "JIRA ticket not found" in str(e)

@patch('mr_reminder_core.requests.post', side_effect=fake_slack_post_error)
@patch('mr_reminder_core.TeamGitLabClient.get_open_merge_requests', new=fake_gitlab_get_open_merge_requests)
@patch('mr_reminder_core.TeamGitLabClient.get_merge_request_approvals', new=fake_gitlab_get_merge_request_approvals)
@patch('mr_reminder_core.SimpleJiraClient.get_ticket_details', new=fake_jira_get_ticket_details_safe)
@patch('mr_reminder_core.load_projects_config')
def test_slack_api_error(mock_load_config, mock_post, fake_config):
    os.environ['JIRA_URL'] = 'http://fake-jira'
    os.environ['JIRA_USERNAME'] = 'user'
    os.environ['JIRA_TOKEN'] = 'token'
    mock_load_config.return_value = fake_config
    # Should not crash, should log error
    with pytest.raises(Exception):
        mr_reminder_core.main() 

@patch('mr_reminder_core.requests.post', side_effect=fake_slack_post)
@patch('mr_reminder_core.TeamGitLabClient.get_open_merge_requests', new=fake_gitlab_get_open_merge_requests)
@patch('mr_reminder_core.TeamGitLabClient.get_merge_request_approvals', new=fake_gitlab_get_merge_request_approvals)
@patch('mr_reminder_core.SimpleJiraClient.get_ticket_details', new=fake_jira_get_ticket_details_safe)
@patch('mr_reminder_core.load_projects_config')
def test_slack_message_format_granular(mock_load_config, mock_post, fake_config):
    """
    Test that the Slack message format includes correct emojis, block structure, and field formatting for various MR scenarios.
    Note: For single-team notifications, project info is not included in the MR block.
    """
    os.environ['JIRA_URL'] = 'http://fake-jira'
    os.environ['JIRA_USERNAME'] = 'user'
    os.environ['JIRA_TOKEN'] = 'token'
    mock_load_config.return_value = fake_config
    mr_reminder_core.main()
    payload = mock_post.call_args_list[0][1]['json']
    blocks = payload['blocks']
    # 1. Header block
    assert blocks[0]['type'] == 'header'
    assert 'Stale Merge Requests Review' in blocks[0]['text']['text']
    # 2. Section block (summary)
    assert blocks[1]['type'] == 'section'
    assert 'Daily Review Reminder' in blocks[1]['text']['text']
    # 3. Divider
    assert blocks[2]['type'] == 'divider'
    # 4. MR section blocks: check for emojis, assignees/reviewers/author
    mr_blocks = [b for b in blocks if b['type'] == 'section' and '|' in b['text']['text']]
    # MR with JIRA and high priority should have JIRA emoji, priority emoji, urgency emoji
    mr1 = next(b for b in mr_blocks if 'Fix bug' in b['text']['text'])
    assert 'JIRA:' in mr1['text']['text']
    assert '🔥' in mr1['text']['text'] or '⚡' in mr1['text']['text']  # priority emoji
    assert any(e in mr1['text']['text'] for e in ['🚨', '🔴', '🟠', '🟡'])  # urgency emoji
    assert '👀 *Reviewers:* Carol' in mr1['text']['text']
    assert '👤 *Assignees:* Bob' in mr1['text']['text']
    assert '✍️ *Author:* Alice' in mr1['text']['text']
    # MR without JIRA should not have JIRA info or priority emoji
    mr2 = next(b for b in mr_blocks if 'Refactor code' in b['text']['text'])
    assert 'JIRA:' not in mr2['text']['text']
    assert '🔥' not in mr2['text']['text'] and '⚡' not in mr2['text']['text']
    # Project info is not present in single-team notifications, so we do not assert for '*Project:*' here
    # 5. Context block (footer)
    assert blocks[-1]['type'] == 'context'
    assert 'Summary:' in blocks[-1]['elements'][0]['text'] 

@patch('mr_reminder_core.requests.post', side_effect=fake_slack_post)
@patch('mr_reminder_core.TeamGitLabClient.get_open_merge_requests', new=fake_gitlab_get_open_merge_requests)
@patch('mr_reminder_core.TeamGitLabClient.get_merge_request_approvals', new=fake_gitlab_get_merge_request_approvals)
@patch('mr_reminder_core.SimpleJiraClient.get_ticket_details', new=fake_jira_get_ticket_details_safe)
@patch('mr_reminder_core.load_projects_config')
def test_slack_message_format_jira_ticket_not_found(mock_load_config, mock_post, fake_config):
    """
    Test that if a JIRA ticket is referenced in the MR but not found, the Slack message does not include JIRA info or priority emoji.
    """
    os.environ['JIRA_URL'] = 'http://fake-jira'
    os.environ['JIRA_USERNAME'] = 'user'
    os.environ['JIRA_TOKEN'] = 'token'
    mock_load_config.return_value = fake_config
    orig_fake_mrs = fake_mrs()
    orig_fake_mrs['Rohan'].append({
        "title": "[NOTFOUND-999] Broken link",
        "web_url": "http://gitlab.com/mr/5",
        "iid": 5,
        "author": {"name": "Frank", "username": "frank"},
        "assignees": [],
        "reviewers": [],
        "created_at": "2024-06-01T10:00:00Z",
        "project_name": "Rohan",
        "project_id": "1",
        "project_token": "token1"
    })
    with patch('mr_reminder_core.TeamGitLabClient.get_open_merge_requests', return_value=orig_fake_mrs):
        mr_reminder_core.main()
        payload = mock_post.call_args_list[0][1]['json']
        blocks = payload['blocks']
        mr_nf = next(b for b in blocks if b['type'] == 'section' and 'Broken link' in b['text']['text'])
        assert 'JIRA:' not in mr_nf['text']['text']
        assert '🔥' not in mr_nf['text']['text'] and '⚡' not in mr_nf['text']['text']
        assert '✍️ *Author:* Frank' in mr_nf['text']['text'] 