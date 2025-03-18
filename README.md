# Woot Deals Tracker

A Google Cloud Run service that monitors Woot.com for deals matching your keywords (e-readers, Kindle, etc.) and sends email notifications when new matching deals are found.

## Overview

This service:
- Periodically checks the Woot API for new deals
- Filters deals based on configurable keywords
- Sends email notifications for matching deals
- Tracks previously seen deals to avoid duplicates
- Runs on Google Cloud Platform's free tier

## Files

- `main.py` - The main application code
- `Dockerfile` - Container configuration for Cloud Run
- `requirements.txt` - Python dependencies
- `test_service.py` - Testing script for the deployed service
- `test_api_endpoints.py` - Script to test Woot API connectivity

## Setup Instructions

### Prerequisites

1. A Google Cloud Platform account
2. A Woot API key (register at developer.woot.com)
3. A Gmail account with App Password configured

### Google Cloud Setup

1. Create a new Google Cloud project
2. Enable the following APIs:
   - Cloud Run API
   - Cloud Scheduler API
   - Cloud Storage API
   - Cloud Build API

3. Create a Cloud Storage bucket:
   ```
   gsutil mb -l REGION gs://YOUR-BUCKET-NAME
   ```

4. Deploy to Cloud Run:
   ```
   gcloud run deploy woot-deals \
     --source . \
     --platform managed \
     --region REGION \
     --memory 256Mi \
     --min-instances 0 \
     --max-instances 1 \
     --set-env-vars="WOOT_API_KEY=your-api-key,GMAIL_USER=your-email@gmail.com,GMAIL_APP_PASSWORD=your-app-password,EMAIL_RECIPIENT=recipient@example.com,BUCKET_NAME=your-bucket-name" \
     --allow-unauthenticated
   ```

5. Set up Cloud Scheduler:
   ```
   gcloud scheduler jobs create http woot-deals-hourly \
     --schedule="0 * * * *" \
     --uri="https://YOUR-CLOUD-RUN-URL" \
     --http-method=GET \
     --location=REGION
   ```

### Testing

After deployment, test the service using:

```
python test_service.py --url YOUR-CLOUD-RUN-URL --test all
```

To test only the API connectivity:

```
python test_api_endpoints.py --api-key YOUR-WOOT-API-KEY
```

## Configuration

Modify the `KEYWORDS` list in `main.py` to customize which products you're interested in.

## Monitoring

Check the Cloud Run logs for service activity:

```
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=woot-deals" --limit 50
```

## Troubleshooting

If the service isn't working as expected:

1. Check if all environment variables are set correctly
2. Verify the Cloud Storage bucket exists and is accessible
3. Test the API connectivity using `test_api_endpoints.py`
4. Check the logs for detailed error messages