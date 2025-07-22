# mr_reminder_core.py
"""
Stale MR Reminder Bot - Core Logic
Sends daily Slack notifications for merge requests pending review for more than 2 days
Multi-project support with priority-based thresholds
"""

import os
import requests
import json
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import logging
import yaml

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)


def load_projects_config(config_path: str = "projects_config.yaml") -> dict:
    """Load the multi-team, multi-project config from YAML."""
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


class TeamConfig:
    """Holds per-team config loaded from YAML."""
    def __init__(self, name: str, data: dict):
        self.name = name
        self.slack_webhook_url = data["slack_webhook_url"]
        self.threshold_config = data["threshold_config"]
        self.gitlab_projects = data["gitlab_projects"]


class TeamGitLabClient:
    """GitLab API client for a team (multiple projects, per-project tokens)."""
    def __init__(self, team_config: TeamConfig, gitlab_url: str):
        self.projects = team_config.gitlab_projects
        self.gitlab_url = gitlab_url

    def get_open_merge_requests(self) -> dict:
        """Fetch open MRs for all projects in the team."""
        all_mrs = {}
        for project_name, project in self.projects.items():
            project_id = project["gitlab_project_id"]
            token = project["gitlab_token"]
            url = f"{self.gitlab_url}/api/v4/projects/{project_id}/merge_requests"
            headers = {"Private-Token": token}
            params = {"state": "opened", "per_page": 100}
            try:
                resp = requests.get(url, headers=headers, params=params)
                resp.raise_for_status()
                mrs = resp.json()
                for mr in mrs:
                    mr["project_name"] = project_name
                    mr["project_id"] = project_id
                    mr["project_token"] = token
                all_mrs[project_name] = mrs
            except requests.RequestException as e:
                logger.error(f"Failed to fetch MRs for {project_name}: {e}")
                all_mrs[project_name] = []
        return all_mrs

    def get_merge_request_approvals(self, project_id: str, mr_iid: int, token: str) -> dict:
        url = f"{self.gitlab_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/approvals"
        headers = {"Private-Token": token}
        try:
            resp = requests.get(url, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.warning(f"Failed to fetch approval status for MR {mr_iid} in project {project_id}: {e}")
            return {}


class TeamMRAnalyzer:
    """Analyze MRs for a team, using per-team thresholds."""
    def __init__(self, team_config: TeamConfig, gitlab_url: str, jira_client):
        self.team_config = team_config
        self.gitlab = TeamGitLabClient(team_config, gitlab_url)
        self.jira = jira_client
        self.thresholds = team_config.threshold_config

    def get_threshold_for_priority(self, priority: str) -> int:
        if not self.thresholds.get("use_priority_thresholds", True) or not priority:
            return self.thresholds["stale_days_threshold"]
        return self.thresholds.get(f"threshold_{priority.lower()}", self.thresholds["stale_days_threshold"])

    def is_mr_stale(self, created_at: str, priority: str = None) -> bool:
        created_date = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
        threshold_days = self.get_threshold_for_priority(priority)
        threshold_date = datetime.now().replace(tzinfo=created_date.tzinfo) - timedelta(days=threshold_days)
        return created_date < threshold_date

    def is_mr_approved(self, project_id: str, mr_iid: int, token: str) -> bool:
        approvals = self.gitlab.get_merge_request_approvals(project_id, mr_iid, token)
        if not approvals:
            return False
        approved_by = approvals.get('approved_by', [])
        return len(approved_by) > 0

    def is_bot_or_dependency_mr(self, mr: dict) -> bool:
        # Use same logic as before, or refactor as needed
        title = mr['title'].lower()
        author_name = mr['author']['name'].lower()
        author_username = mr['author']['username'].lower()
        bot_indicators = [
            'dependabot', 'renovate', 'greenkeeper', 'snyk', 'whitesource',
            'github-actions', 'gitlab-ci', 'automated', 'bot', 'dependency',
            'dependent_pat', 'dependencybot', 'auto-update'
        ]
        for bot_indicator in bot_indicators:
            if bot_indicator in author_name or bot_indicator in author_username:
                return True
        dependency_patterns = [
            'build(deps)', 'build(deps-dev)', 'chore(deps)', 'deps:',
            'bump ', 'update dependencies', 'upgrade dependencies',
            'security update', 'npm audit fix', 'yarn upgrade',
            'pip upgrade', 'requirements update', 'package update',
            'version bump', 'dependency update', 'auto-update',
            'automated update', '[security]', 'security patch'
        ]
        for pattern in dependency_patterns:
            if pattern in title:
                return True
        return False

    def extract_jira_ticket(self, mr_title: str, mr_description: str) -> str:
        import re
        patterns = [r'([A-Z]+-\d+)', r'\[([A-Z]+-\d+)\]']
        text = f"{mr_title} {mr_description}"
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1)
        return None

    def get_stale_mrs(self) -> list:
        all_open_mrs = self.gitlab.get_open_merge_requests()
        stale_mrs = []
        for project_name, mrs in all_open_mrs.items():
            for mr in mrs:
                jira_ticket = self.extract_jira_ticket(mr['title'], mr.get('description', ''))
                jira_details = {'status': None, 'priority': None, 'priority_id': None}
                if jira_ticket:
                    jira_details = self.jira.get_ticket_details(jira_ticket)
                if not self.is_mr_stale(mr['created_at'], jira_details['priority']):
                    continue
                if self.is_mr_approved(mr['project_id'], mr['iid'], mr.get('project_token')):
                    continue
                if mr.get('draft', False) or 'WIP:' in mr['title'] or 'Draft:' in mr['title']:
                    continue
                if self.is_bot_or_dependency_mr(mr):
                    continue
                created_date = datetime.fromisoformat(mr['created_at'].replace('Z', '+00:00'))
                days_old = (datetime.now().replace(tzinfo=created_date.tzinfo) - created_date).days
                applicable_threshold = self.get_threshold_for_priority(jira_details['priority'])
                stale_mr = {
                    'title': mr['title'],
                    'web_url': mr['web_url'],
                    'iid': mr['iid'],
                    'author': mr['author']['name'],
                    'assignees': [assignee['name'] for assignee in mr.get('assignees', [])],
                    'reviewers': [reviewer['name'] for reviewer in mr.get('reviewers', [])],
                    'days_old': days_old,
                    'jira_ticket': jira_ticket,
                    'jira_status': jira_details['status'],
                    'jira_priority': jira_details['priority'],
                    'threshold_used': applicable_threshold,
                    'created_at': mr['created_at'],
                    'project_name': project_name,
                    'project_id': mr['project_id']
                }
                stale_mrs.append(stale_mr)
        return stale_mrs


class SlackNotifier:
    """Slack notification handler"""
    
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
    
    def format_mr_message(self, mrs: List[Dict]) -> Dict:
        """Format stale MRs into a beautiful Slack message (backward compatibility)"""
        if not mrs:
            return {
                "text": "🎉 Great news! No stale merge requests found. All reviews are up to date!",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "🎉 *Great news!* No stale merge requests found. All reviews are up to date!"
                        }
                    }
                ]
            }
        
        # Sort MRs by days old (oldest first)
        mrs.sort(key=lambda x: x['days_old'], reverse=True)
        
        # Header message
        count = len(mrs)
        header_text = f"🔔 *Daily Review Reminder* - {count} merge request{'s' if count != 1 else ''} need{'s' if count == 1 else ''} attention"
        
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "🔍 Stale Merge Requests Review"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": header_text
                }
            },
            {
                "type": "divider"
            }
        ]
        
        # Add each MR as a section
        for mr in mrs:
            # Create assignee/reviewer text
            people_text = ""
            if mr['reviewers']:
                people_text += f"👀 *Reviewers:* {', '.join(mr['reviewers'])}\n"
            if mr['assignees']:
                people_text += f"👤 *Assignees:* {', '.join(mr['assignees'])}\n"
            
            people_text += f"✍️ *Author:* {mr['author']}"
            
            # JIRA info with priority
            jira_text = ""
            # Only include JIRA info if ticket is found (status or priority is not None)
            if mr.get('jira_ticket') and (mr.get('jira_status') or mr.get('jira_priority')):
                jira_text = f"🎫 *JIRA:* {mr['jira_ticket']}"
                if mr.get('jira_status'):
                    jira_text += f" ({mr['jira_status']})"
                if mr.get('jira_priority'):
                    priority_emoji = self._get_priority_emoji(mr['jira_priority'])
                    jira_text += f" {priority_emoji} {mr['jira_priority'].title()}"
                jira_text += "\n"
            # If no jira_ticket, jira_text remains empty and is not included

            # Urgency indicator (enhanced with priority consideration)
            urgency_emoji = self._get_urgency_emoji(mr['days_old'], mr['threshold_used'], mr.get('jira_priority'))
            
            # Add project info if available
            project_info = ""
            if mr.get('project_name'):
                project_emoji = self._get_project_emoji(mr['project_name'])
                project_info = f"{project_emoji} *Project:* {mr['project_name']}\n"
            
            mr_block = {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{urgency_emoji} *<{mr['web_url']}|{mr['title'][:60]}{'...' if len(mr['title']) > 60 else ''}>*\n"
                            f"⏰ *Age:* {mr['days_old']} day{'s' if mr['days_old'] != 1 else ''} old "
                            f"(threshold: {mr['threshold_used']} day{'s' if mr['threshold_used'] != 1 else ''})\n"
                            f"{project_info}"
                            f"{jira_text if jira_text else ''}"
                            f"{people_text}"
                }
            }
            blocks.append(mr_block)
            blocks.append({"type": "divider"})
        
        # Footer with summary
        footer_text = f"📊 *Summary:* {count} MR{'s' if count != 1 else ''} pending review • "
        footer_text += f"Oldest: {max(mr['days_old'] for mr in mrs)} days • "
        footer_text += f"Average age: {sum(mr['days_old'] for mr in mrs) // count} days"
        
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": footer_text
                }
            ]
        })
        
        return {
            "text": header_text,  # Fallback text for notifications
            "blocks": blocks
        }

    def format_multi_project_message(self, mrs_by_project: Dict[str, List[Dict]]) -> Dict:
        """Format stale MRs from multiple projects into a beautiful Slack message"""
        if not mrs_by_project:
            return {
                "text": "🎉 Great news! No stale merge requests found across all projects. All reviews are up to date!",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "🎉 *Great news!* No stale merge requests found across all projects. All reviews are up to date!"
                        }
                    }
                ]
            }
        
        # Calculate totals
        total_mrs = sum(len(mrs) for mrs in mrs_by_project.values())
        all_mrs = []
        for mrs in mrs_by_project.values():
            all_mrs.extend(mrs)
        
        # Header message
        project_count = len(mrs_by_project)
        header_text = f"🔔 *Daily Review Reminder* - {total_mrs} merge request{'s' if total_mrs != 1 else ''} need attention across {project_count} project{'s' if project_count != 1 else ''}"
        
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "🔍 Stale Merge Requests Review"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": header_text
                }
            },
            {
                "type": "divider"
            }
        ]
        
        # Add each project section
        for project_name, mrs in mrs_by_project.items():
            # Sort MRs by days old (oldest first) within each project
            mrs.sort(key=lambda x: x['days_old'], reverse=True)
            
            # Project header
            project_emoji = self._get_project_emoji(project_name)
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{project_emoji} *{project_name}* - {len(mrs)} MR{'s' if len(mrs) != 1 else ''}"
                }
            })
            
            # Add each MR in this project
            for mr in mrs:
                # Create assignee/reviewer text
                people_text = ""
                if mr['reviewers']:
                    people_text += f"👀 *Reviewers:* {', '.join(mr['reviewers'])}\n"
                if mr['assignees']:
                    people_text += f"👤 *Assignees:* {', '.join(mr['assignees'])}\n"
                
                people_text += f"✍️ *Author:* {mr['author']}"
                
                # JIRA info with priority
                jira_text = ""
                # Only include JIRA info if ticket is found (status or priority is not None)
                if mr.get('jira_ticket') and (mr.get('jira_status') or mr.get('jira_priority')):
                    jira_text = f"🎫 *JIRA:* {mr['jira_ticket']}"
                    if mr.get('jira_status'):
                        jira_text += f" ({mr['jira_status']})"
                    if mr.get('jira_priority'):
                        priority_emoji = self._get_priority_emoji(mr['jira_priority'])
                        jira_text += f" {priority_emoji} {mr['jira_priority'].title()}"
                    jira_text += "\n"
                # If no jira_ticket, jira_text remains empty and is not included
                
                # Urgency indicator (enhanced with priority consideration)
                urgency_emoji = self._get_urgency_emoji(mr['days_old'], mr['threshold_used'], mr.get('jira_priority'))
                
                mr_block = {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"    {urgency_emoji} *<{mr['web_url']}|{mr['title'][:55]}{'...' if len(mr['title']) > 55 else ''}>*\n"
                                f"    ⏰ *Age:* {mr['days_old']} day{'s' if mr['days_old'] != 1 else ''} old "
                                f"(threshold: {mr['threshold_used']} day{'s' if mr['threshold_used'] != 1 else ''})\n"
                                f"    {jira_text.replace(chr(10), chr(10) + '    ') if jira_text else ''}"
                                f"    {people_text.replace(chr(10), chr(10) + '    ')}"
                }
                }
                blocks.append(mr_block)
            
            # Add separator between projects
            blocks.append({"type": "divider"})
        
        # Footer with summary
        if all_mrs:
            oldest_mr = max(all_mrs, key=lambda x: x['days_old'])
            avg_age = sum(mr['days_old'] for mr in all_mrs) // len(all_mrs)
            
            footer_text = f"📊 *Summary:* {total_mrs} MR{'s' if total_mrs != 1 else ''} across {project_count} project{'s' if project_count != 1 else ''} • "
            footer_text += f"Oldest: {oldest_mr['days_old']} days ({oldest_mr['project_name']}) • "
            footer_text += f"Average age: {avg_age} days"
            
            blocks.append({
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": footer_text
                    }
                ]
            })
        
        return {
            "text": header_text,  # Fallback text for notifications
            "blocks": blocks
        }
    
    def _get_project_emoji(self, project_name: str) -> str:
        """Get emoji for project name"""
        project_emojis = {
            'rohan': '🏰',
            'edoras': '🏛️', 
            'athena': '🦉',
            'backend': '⚙️',
            'frontend': '🎨',
            'api': '🔌',
            'web': '🌐',
            'mobile': '📱',
            'admin': '👑',
            'core': '💎'
        }
        
        # Try to match project name (case insensitive)
        for key, emoji in project_emojis.items():
            if key in project_name.lower():
                return emoji
        
        return '📁'  # Default project emoji
    
    def format_single_project_message(self, mrs: List[Dict]) -> Dict:
        """Format stale MRs into a beautiful Slack message"""
        if not mrs:
            return {
                "text": "🎉 Great news! No stale merge requests found. All reviews are up to date!",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "🎉 *Great news!* No stale merge requests found. All reviews are up to date!"
                        }
                    }
                ]
            }
        
        # Sort MRs by days old (oldest first)
        mrs.sort(key=lambda x: x['days_old'], reverse=True)
        
        # Header message
        count = len(mrs)
        header_text = f"🔔 *Daily Review Reminder* - {count} merge request{'s' if count != 1 else ''} need{'s' if count == 1 else ''} attention"
        
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "🔍 Stale Merge Requests Review"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": header_text
                }
            },
            {
                "type": "divider"
            }
        ]
        
        # Add each MR as a section
        for mr in mrs:
            # Create assignee/reviewer text
            people_text = ""
            if mr['reviewers']:
                people_text += f"👀 *Reviewers:* {', '.join(mr['reviewers'])}\n"
            if mr['assignees']:
                people_text += f"👤 *Assignees:* {', '.join(mr['assignees'])}\n"
            
            people_text += f"✍️ *Author:* {mr['author']}"
            
            # JIRA info with priority
            jira_text = ""
            if mr['jira_ticket']:
                jira_text = f"🎫 *JIRA:* {mr['jira_ticket']}"
                if mr['jira_status']:
                    jira_text += f" ({mr['jira_status']})"
                if mr['jira_priority']:
                    priority_emoji = self._get_priority_emoji(mr['jira_priority'])
                    jira_text += f" {priority_emoji} {mr['jira_priority'].title()}"
                jira_text += "\n"
            
            # Urgency indicator (enhanced with priority consideration)
            urgency_emoji = self._get_urgency_emoji(mr['days_old'], mr['threshold_used'], mr.get('jira_priority'))
            
            mr_block = {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{urgency_emoji} *<{mr['web_url']}|{mr['title'][:60]}{'...' if len(mr['title']) > 60 else ''}>*\n"
                            f"⏰ *Age:* {mr['days_old']} day{'s' if mr['days_old'] != 1 else ''} old "
                            f"(threshold: {mr['threshold_used']} day{'s' if mr['threshold_used'] != 1 else ''})\n"
                            f"{jira_text}"
                            f"{people_text}"
                }
            }
            blocks.append(mr_block)
            blocks.append({"type": "divider"})
        
        # Footer with summary
        footer_text = f"📊 *Summary:* {count} MR{'s' if count != 1 else ''} pending review • "
        footer_text += f"Oldest: {max(mr['days_old'] for mr in mrs)} days • "
        footer_text += f"Average age: {sum(mr['days_old'] for mr in mrs) // count} days"
        
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": footer_text
                }
            ]
        })
        
        return {
            "text": header_text,  # Fallback text for notifications
            "blocks": blocks
        }
    
    def _get_priority_emoji(self, priority: str) -> str:
        """Get emoji for JIRA priority"""
        priority_emojis = {
            'highest': '🔥',
            'high': '⚡',
            'medium': '📋',
            'low': '📝',
            'lowest': '💤'
        }
        return priority_emojis.get(priority.lower(), '📋')
    
    def _get_urgency_emoji(self, days_old: int, threshold: int, priority: Optional[str]) -> str:
        """Get urgency emoji based on age, threshold, and priority"""
        # Calculate how much over threshold the MR is
        days_over_threshold = days_old - threshold
        
        # Priority-based urgency
        if priority in ['highest', 'high']:
            if days_over_threshold >= 2:
                return "🚨"  # Critical - high priority way overdue
            elif days_over_threshold >= 1:
                return "🔴"  # Red - high priority overdue
            else:
                return "🟠"  # Orange - high priority approaching threshold
        else:
            # Standard urgency for medium/low/no priority
            if days_over_threshold >= 3:
                return "🔴"  # Red - way overdue
            elif days_over_threshold >= 1:
                return "🟠"  # Orange - overdue
            else:
                return "🟡"  # Yellow - approaching threshold
    
    def send_notification(self, message: Dict) -> bool:
        """Send notification to Slack"""
        try:
            response = requests.post(
                self.webhook_url,
                json=message,
                headers={'Content-Type': 'application/json'}
            )
            response.raise_for_status()
            logger.info("Slack notification sent successfully")
            return True
        except requests.RequestException as e:
            logger.error(f"Failed to send Slack notification: {e}")
            return False


class SimpleJiraClient:
    def __init__(self, url, username, token):
        self.base_url = url
        self.auth = (username, token)
    def get_ticket_details(self, ticket_key: str) -> dict:
        url = f"{self.base_url}/rest/api/2/issue/{ticket_key}"
        try:
            response = requests.get(url, auth=self.auth)
            response.raise_for_status()
            ticket_data = response.json()
            return {
                'status': ticket_data['fields']['status']['name'],
                'priority': ticket_data['fields']['priority']['name'].lower() if ticket_data['fields']['priority'] else None,
                'priority_id': ticket_data['fields']['priority']['id'] if ticket_data['fields']['priority'] else None
            }
        except requests.RequestException as e:
            logger.warning(f"Failed to fetch JIRA ticket {ticket_key}: {e}")
            return {'status': None, 'priority': None, 'priority_id': None}


def main():
    """Main execution function"""
    try:
        # Load global JIRA config from env
        jira_url = os.getenv('JIRA_URL')
        jira_username = os.getenv('JIRA_USERNAME')
        jira_token = os.getenv('JIRA_TOKEN')
        if not (jira_url and jira_username and jira_token):
            raise ValueError("Missing required JIRA environment variables")
        jira_client = SimpleJiraClient(jira_url, jira_username, jira_token)
        # Load team/project config
        teams_data = load_projects_config()
        for team_name, team_data in teams_data.items():
            logger.info(f"Processing team: {team_name}")
            team_config = TeamConfig(team_name, team_data)
            analyzer = TeamMRAnalyzer(team_config, os.getenv('GITLAB_URL', 'https://gitlab.com'), jira_client)
            stale_mrs = analyzer.get_stale_mrs()
            if not stale_mrs:
                logger.info(f"No stale MRs for team {team_name}")
                continue
            # Group stale MRs by project name for Slack message
            mrs_by_project = {}
            for mr in stale_mrs:
                pname = mr['project_name']
                mrs_by_project.setdefault(pname, []).append(mr)
            notifier = SlackNotifier(team_config.slack_webhook_url)
            message = notifier.format_multi_project_message(mrs_by_project)
            success = notifier.send_notification(message)
            if success:
                logger.info(f"Slack notification sent for team {team_name}")
            else:
                logger.error(f"Failed to send Slack notification for team {team_name}")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise


if __name__ == "__main__":
    main()