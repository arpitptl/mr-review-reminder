#!/bin/bash

echo "🚀 Deploying Stale MR Reminder to AWS Lambda..."

# Check if .env file exists
if [ ! -f .env ]; then
    echo "❌ Error: .env file not found!"
    echo "Please create .env file with your JIRA configuration (JIRA_URL, JIRA_USERNAME, JIRA_TOKEN)"
    exit 1
fi

# Check if projects_config.yaml exists
if [ ! -f projects_config.yaml ]; then
    echo "❌ Error: projects_config.yaml file not found!"
    echo "Please create projects_config.yaml (see projects_config.example.yaml for format)"
    exit 1
fi

# Source environment variables
set -a
source .env
set +a

# Validate required environment variables
required_vars=("JIRA_URL" "JIRA_USERNAME" "JIRA_TOKEN")
for var in "${required_vars[@]}"; do
    if [ -z "${!var}" ]; then
        echo "❌ Error: Required environment variable $var is not set"
        exit 1
    fi
done

echo "✅ Environment variables validated"
echo "✅ projects_config.yaml found"
echo "ℹ️  To onboard a new team, just edit projects_config.yaml and redeploy."

# Choose deployment method
echo "Choose deployment method:"
echo "1) Serverless Framework"
echo "2) AWS SAM"
echo "3) Manual ZIP upload"
read -p "Enter choice (1-3): " choice

case $choice in
    1)
        echo "📦 Deploying with Serverless Framework..."
        if ! command -v serverless &> /dev/null; then
            echo "Installing Serverless Framework..."
            npm install -g serverless
        fi
        serverless deploy
        ;;
    2)
        echo "📦 Deploying with AWS SAM..."
        if ! command -v sam &> /dev/null; then
            echo "❌ AWS SAM CLI not found. Please install it first."
            exit 1
        fi
        sam build
        sam deploy --guided
        ;;
    3)
        echo "📦 Creating deployment package..."
        # Create deployment package
        rm -rf package/
        mkdir -p package/
        
        # Copy source files
        cp lambda_function.py package/
        cp mr_reminder_core.py package/
        cp projects_config.yaml package/
        
        # Install dependencies
        pip install -r requirements.txt -t package/
        
        # Create ZIP file
        cd package/
        zip -r ../stale-mr-reminder.zip .
        cd ..
        
        echo "✅ Deployment package created: stale-mr-reminder.zip"
        echo "📋 Next steps:"
        echo "1. Go to AWS Lambda Console"
        echo "2. Create new function or update existing"
        echo "3. Upload stale-mr-reminder.zip"
        echo "4. Set handler to: lambda_function.lambda_handler"
        echo "5. Configure JIRA environment variables"
        echo "6. Ensure projects_config.yaml is present in the deployment package"
        echo "7. Set up EventBridge trigger: cron(30 11 * * ? *)"
        ;;
    *)
        echo "❌ Invalid choice"
        exit 1
        ;;
esac

echo "🎉 Deployment completed!"
