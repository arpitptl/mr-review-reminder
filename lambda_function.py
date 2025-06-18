# lambda_function.py - AWS Lambda Entry Point
"""
AWS Lambda function for Stale MR Reminder Bot
Runs daily at 5 PM IST to send Slack notifications for stale merge requests
"""

import json
import logging
from mr_reminder_core import main as run_mr_reminder

# Configure logging for Lambda
logger = logging.getLogger()
logger.setLevel(logging.INFO)

def lambda_handler(event, context):
    """
    AWS Lambda handler function
    
    Args:
        event: Lambda event data (from EventBridge/CloudWatch Events)
        context: Lambda context object
        
    Returns:
        dict: Response with status code and message
    """
    try:
        logger.info("Starting Stale MR Reminder Lambda execution")
        logger.info(f"Event: {json.dumps(event)}")
        
        # Run the main MR reminder logic
        run_mr_reminder()
        
        logger.info("Successfully completed MR reminder execution")
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Stale MR reminder executed successfully',
                'timestamp': context.aws_request_id
            })
        }
        
    except Exception as e:
        logger.error(f"Error in lambda execution: {str(e)}", exc_info=True)
        
        return {
            'statusCode': 500,
            'body': json.dumps({
                'error': str(e),
                'message': 'Failed to execute MR reminder',
                'timestamp': context.aws_request_id
            })
        }


# For local testing
if __name__ == "__main__":
    # Mock Lambda context for local testing
    class MockContext:
        aws_request_id = "local-test-12345"
        
    # Test the lambda function
    test_event = {"source": "aws.events", "detail-type": "Scheduled Event"}
    result = lambda_handler(test_event, MockContext())
    print(f"Local test result: {result}")