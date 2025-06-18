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

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)


class Config:
    """Configuration management"""
    
    def __init__(self):
        self.gitlab_url = os.getenv('GITLAB_URL', 'https://gitlab.com')
        self.gitlab_token = os.getenv('GITLAB_TOKEN')
        self.jira_url = os.getenv('JIRA_URL')
        self.jira_username = os.getenv('JIRA_USERNAME')
        self.jira_token = os.getenv('JIRA_TOKEN')
        self.slack_webhook_url = os.getenv('SLACK_WEBHOOK_URL')
        
        # Multi-project support
        self.projects = self._parse_projects()
        
        self.stale_days_threshold = int(os.getenv('STALE_DAYS_THRESHOLD', '2'))
        self.exclude_bots = os.getenv('EXCLUDE_BOTS', 'true').lower() == 'true'
        self.exclude_dependencies = os.getenv('EXCLUDE_DEPENDENCIES', 'true').lower() == 'true'
        self.custom_bot_keywords = os.getenv('CUSTOM_BOT_KEYWORDS', '').split(',') if os.getenv('CUSTOM_BOT_KEYWORDS') else []
        self.custom_dependency_keywords = os.getenv('CUSTOM_DEPENDENCY_KEYWORDS', '').split(',') if os.getenv('CUSTOM_DEPENDENCY_KEYWORDS') else []
        
        # Priority-based thresholds
        self.use_priority_thresholds = os.getenv('USE_PRIORITY_THRESHOLDS', 'true').lower() == 'true'
        self.priority_thresholds = {
            'highest': int(os.getenv('THRESHOLD_HIGHEST', '1')),
            'high': int(os.getenv('THRESHOLD_HIGH', '2')),
            'medium': int(os.getenv('THRESHOLD_MEDIUM', '3')),
            'low': int(os.getenv('THRESHOLD_LOW', '3')),
            'lowest': int(os.getenv('THRESHOLD_LOWEST', '3'))
        }
        
    def _parse_projects(self):
        """Parse projects from environment variables"""
        projects = []
        
        # Support both single project (backward compatibility) and multi-project
        single_project_id = os.getenv('GITLAB_PROJECT_ID')
        if single_project_id:
            projects.append({
                'id': single_project_id,
                'name': os.getenv('GITLAB_PROJECT_NAME', 'Default Project'),
                'token': self.gitlab_token  # Use global token for single project
            })
        
        # Multi-project configuration
        projects_config = os.getenv('GITLAB_PROJECTS')
        if projects_config:
            # Format: "id1:name1:token1,id2:name2:token2,id3:name3:token3"
            # Example: "123:Rohan:glpat-xxx,456:Edoras:glpat-yyy,789:Athena:glpat-zzz"
            # OR: "123:Rohan,456:Edoras,789:Athena" (uses global token)
            for project_config in projects_config.split(','):
                parts = project_config.strip().split(':')
                
                if len(parts) == 3:
                    # Format: id:name:token
                    project_id, project_name, project_token = parts
                    projects.append({
                        'id': project_id.strip(),
                        'name': project_name.strip(),
                        'token': project_token.strip()
                    })
                elif len(parts) == 2:
                    # Format: id:name (uses global token)
                    project_id, project_name = parts
                    projects.append({
                        'id': project_id.strip(),
                        'name': project_name.strip(),
                        'token': self.gitlab_token
                    })
                elif len(parts) == 1:
                    # Format: id (uses global token, ID as name)
                    project_id = parts[0].strip()
                    projects.append({
                        'id': project_id,
                        'name': f'Project {project_id}',
                        'token': self.gitlab_token
                    })
        
        return projects
        
    def validate(self):
        """Validate required configuration"""
        required_vars = [
            'JIRA_URL', 'JIRA_USERNAME', 
            'JIRA_TOKEN', 'SLACK_WEBHOOK_URL'
        ]
        
        missing = [var for var in required_vars if not getattr(self, var.lower())]
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
            
        # Validate projects configuration
        if not self.projects:
            raise ValueError("No projects configured. Set either GITLAB_PROJECT_ID or GITLAB_PROJECTS")


class GitLabClient:
    """GitLab API client with multi-project support and per-project tokens"""
    
    def __init__(self, config: Config):
        self.base_url = f"{config.gitlab_url}/api/v4"
        self.default_token = config.gitlab_token
        self.projects = config.projects
        
    def _get_headers_for_project(self, project: Dict) -> Dict:
        """Get headers with appropriate token for the project"""
        token = project.get('token', self.default_token)
        return {'Private-Token': token}
        
    def get_open_merge_requests_for_project(self, project: Dict) -> List[Dict]:
        """Fetch all open merge requests for a specific project"""
        project_id = project['id']
        headers = self._get_headers_for_project(project)
        
        url = f"{self.base_url}/projects/{project_id}/merge_requests"
        params = {
            'state': 'opened',
            'per_page': 100
        }
        
        try:
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Failed to fetch GitLab MRs for project {project_id}: {e}")
            return []
    
    def get_all_open_merge_requests(self) -> Dict[str, List[Dict]]:
        """Fetch open merge requests for all configured projects"""
        all_mrs = {}
        
        for project in self.projects:
            project_id = project['id']
            project_name = project['name']
            
            logger.info(f"Fetching MRs for project: {project_name} (ID: {project_id})")
            mrs = self.get_open_merge_requests_for_project(project)
            
            # Add project info to each MR
            for mr in mrs:
                mr['project_name'] = project_name
                mr['project_id'] = project_id
                mr['project_token'] = project.get('token', self.default_token)
            
            all_mrs[project_name] = mrs
            logger.info(f"Found {len(mrs)} open MRs in {project_name}")
        
        return all_mrs
    
    def get_merge_request_approvals(self, project_id: str, mr_iid: int, token: str = None) -> Dict:
        """Get approval status for a specific MR"""
        # Use provided token or find project token
        if not token:
            # Find the project to get its token
            project = next((p for p in self.projects if p['id'] == project_id), None)
            token = project.get('token', self.default_token) if project else self.default_token
            
        headers = {'Private-Token': token}
        url = f"{self.base_url}/projects/{project_id}/merge_requests/{mr_iid}/approvals"
        
        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.warning(f"Failed to fetch approval status for MR {mr_iid} in project {project_id}: {e}")
            return {}


class JiraClient:
    """JIRA API client"""
    
    def __init__(self, config: Config):
        self.base_url = config.jira_url
        self.auth = (config.jira_username, config.jira_token)
        
    def get_ticket_details(self, ticket_key: str) -> Dict:
        """Get JIRA ticket details including status and priority"""
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


class MRAnalyzer:
    """Analyze merge requests for stale reviews"""
    
    def __init__(self, config: Config):
        self.config = config
        self.gitlab = GitLabClient(config)
        self.jira = JiraClient(config)
        
    def extract_jira_ticket(self, mr_title: str, mr_description: str) -> Optional[str]:
        """Extract JIRA ticket key from MR title or description"""
        import re
        
        # Common patterns for JIRA tickets
        patterns = [
            r'([A-Z]+-\d+)',  # Standard JIRA format
            r'\[([A-Z]+-\d+)\]',  # Bracketed format
        ]
        
        text = f"{mr_title} {mr_description}"
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1)
        return None
    
    def get_threshold_for_priority(self, priority: Optional[str]) -> int:
        """Get stale threshold based on JIRA priority"""
        if not self.config.use_priority_thresholds or not priority:
            return self.config.stale_days_threshold
            
        return self.config.priority_thresholds.get(priority.lower(), self.config.stale_days_threshold)
    
    def is_mr_stale(self, created_at: str, priority: Optional[str] = None) -> bool:
        """Check if MR is older than threshold (considering priority)"""
        created_date = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
        threshold_days = self.get_threshold_for_priority(priority)
        threshold_date = datetime.now().replace(tzinfo=created_date.tzinfo) - timedelta(days=threshold_days)
        return created_date < threshold_date
    
    def is_mr_approved(self, project_id: str, mr_iid: int, token: str = None) -> bool:
        """Check if MR has any approvals"""
        approvals = self.gitlab.get_merge_request_approvals(project_id, mr_iid, token)
        if not approvals:
            return False
            
        # Check if there are any approved_by entries
        approved_by = approvals.get('approved_by', [])
        return len(approved_by) > 0
    
    def is_bot_or_dependency_mr(self, mr: Dict) -> bool:
        """Check if MR is created by bots or for dependency updates"""
        if not (self.config.exclude_bots or self.config.exclude_dependencies):
            return False
            
        title = mr['title'].lower()
        author_name = mr['author']['name'].lower()
        author_username = mr['author']['username'].lower()
        
        # Check for bot authors
        if self.config.exclude_bots:
            bot_indicators = [
                'dependabot', 'renovate', 'greenkeeper', 'snyk', 'whitesource',
                'github-actions', 'gitlab-ci', 'automated', 'bot', 'dependency',
                'dependent_pat', 'dependencybot', 'auto-update'
            ]
            
            # Add custom bot keywords
            bot_indicators.extend([keyword.strip().lower() for keyword in self.config.custom_bot_keywords if keyword.strip()])
            
            # Check if author is a bot
            for bot_indicator in bot_indicators:
                if bot_indicator in author_name or bot_indicator in author_username:
                    return True
        
        # Check for dependency updates
        if self.config.exclude_dependencies:
            dependency_patterns = [
                'build(deps)', 'build(deps-dev)', 'chore(deps)', 'deps:',
                'bump ', 'update dependencies', 'upgrade dependencies',
                'security update', 'npm audit fix', 'yarn upgrade',
                'pip upgrade', 'requirements update', 'package update',
                'version bump', 'dependency update', 'auto-update',
                'automated update', '[security]', 'security patch'
            ]
            
            # Add custom dependency keywords
            dependency_patterns.extend([keyword.strip().lower() for keyword in self.config.custom_dependency_keywords if keyword.strip()])
            
            # Check if title indicates dependency update
            for pattern in dependency_patterns:
                if pattern in title:
                    return True
                    
        return False

    def get_stale_mrs(self) -> List[Dict]:
        """Get all stale MRs that need review"""
        open_mrs = self.gitlab.get_open_merge_requests()
        stale_mrs = []
        
        for mr in open_mrs:
            # Extract JIRA ticket first to get priority
            jira_ticket = self.extract_jira_ticket(mr['title'], mr.get('description', ''))
            
            # Get ticket details if available
            jira_details = {'status': None, 'priority': None, 'priority_id': None}
            if jira_ticket:
                jira_details = self.jira.get_ticket_details(jira_ticket)
            
            # Skip if MR is not stale (considering priority)
            if not self.is_mr_stale(mr['created_at'], jira_details['priority']):
                continue
                
            # Skip if MR is already approved
            if self.is_mr_approved(mr['iid']):
                continue
                
            # Skip if MR is in draft state
            if mr.get('draft', False) or 'WIP:' in mr['title'] or 'Draft:' in mr['title']:
                continue
                
            # Skip bot/dependency MRs
            if self.is_bot_or_dependency_mr(mr):
                continue
            
            # Extract JIRA ticket
            jira_ticket = self.extract_jira_ticket(mr['title'], mr.get('description', ''))
            
            # Get ticket details if available
            jira_details = {'status': None, 'priority': None, 'priority_id': None}
            if jira_ticket:
                jira_details = self.jira.get_ticket_details(jira_ticket)
            
            # Calculate days old
            created_date = datetime.fromisoformat(mr['created_at'].replace('Z', '+00:00'))
            days_old = (datetime.now().replace(tzinfo=created_date.tzinfo) - created_date).days
            
            # Get applicable threshold for this MR
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
                'created_at': mr['created_at']
            }
            
            stale_mrs.append(stale_mr)
        
        return stale_mrs

    def get_stale_mrs_by_project(self) -> Dict[str, List[Dict]]:
        """Get all stale MRs grouped by project"""
        all_open_mrs = self.gitlab.get_all_open_merge_requests()
        stale_mrs_by_project = {}
        
        for project_name, mrs in all_open_mrs.items():
            stale_mrs = []
            
            for mr in mrs:
                # Extract JIRA ticket first to get priority
                jira_ticket = self.extract_jira_ticket(mr['title'], mr.get('description', ''))
                
                # Get ticket details if available
                jira_details = {'status': None, 'priority': None, 'priority_id': None}
                if jira_ticket:
                    jira_details = self.jira.get_ticket_details(jira_ticket)
                
                # Skip if MR is not stale (considering priority)
                if not self.is_mr_stale(mr['created_at'], jira_details['priority']):
                    continue
                    
                # Skip if MR is already approved
                if self.is_mr_approved(mr['project_id'], mr['iid'], mr.get('project_token')):
                    continue
                    
                # Skip if MR is in draft state
                if mr.get('draft', False) or 'WIP:' in mr['title'] or 'Draft:' in mr['title']:
                    continue
                    
                # Skip bot/dependency MRs
                if self.is_bot_or_dependency_mr(mr):
                    continue
            
                # Calculate days old
                created_date = datetime.fromisoformat(mr['created_at'].replace('Z', '+00:00'))
                days_old = (datetime.now().replace(tzinfo=created_date.tzinfo) - created_date).days
                
                # Get applicable threshold for this MR
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
            
            # Only include projects that have stale MRs
            if stale_mrs:
                stale_mrs_by_project[project_name] = stale_mrs
                logger.info(f"Found {len(stale_mrs)} stale MRs in {project_name}")
        
        return stale_mrs_by_project
    

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


def main():
    """Main execution function"""
    try:
        # Load and validate configuration
        config = Config()
        config.validate()
        
        logger.info("Starting stale MR analysis...")
        
        # Analyze MRs across all projects
        analyzer = MRAnalyzer(config)
        
        # Check if we have multiple projects configured
        if len(config.projects) > 1:
            # Multi-project mode
            stale_mrs_by_project = analyzer.get_stale_mrs_by_project()
            total_stale_mrs = sum(len(mrs) for mrs in stale_mrs_by_project.values())
            logger.info(f"Found {total_stale_mrs} stale MRs across {len(stale_mrs_by_project)} projects")
            
            # Send multi-project Slack notification
            notifier = SlackNotifier(config.slack_webhook_url)
            message = notifier.format_multi_project_message(stale_mrs_by_project)
        else:
            # Single project mode (backward compatibility)
            stale_mrs = analyzer.get_stale_mrs()
            logger.info(f"Found {len(stale_mrs)} stale MRs")
            
            # Send single-project Slack notification
            notifier = SlackNotifier(config.slack_webhook_url)
            message = notifier.format_mr_message(stale_mrs)
        
        success = notifier.send_notification(message)
        
        if success:
            logger.info("Daily reminder completed successfully")
        else:
            logger.error("Failed to send daily reminder")
            
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise


# if __name__ == "__main__":
#     main()