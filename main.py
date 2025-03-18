import requests
import json
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import os
from google.cloud import storage
import sys
import traceback
from flask import Flask, request

# Set up detailed logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Configuration (use environment variables for sensitive data)
WOOT_API_KEY = os.environ.get("WOOT_API_KEY")
FEED_ENDPOINT = "https://developer.woot.com/feed/Electronics"
GETOFFERS_ENDPOINT = "https://developer.woot.com/getoffers"
KEYWORDS = ["kindle", "ereader", "e-reader", "e-ink", "kobo", "nook", "eink", "treadmill", "walking pad", "iphone"]

# Gmail configuration
GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
EMAIL_RECIPIENT = os.environ.get("EMAIL_RECIPIENT")

# GCS configuration
BUCKET_NAME = os.environ.get("BUCKET_NAME")
SEEN_DEALS_FILENAME = "seen_deals.json"

# Initialize storage client
storage_client = None
try:
    storage_client = storage.Client()
    logging.info("Successfully initialized storage client")
except Exception as e:
    logging.error(f"Error initializing storage client: {e}")
    logging.error(traceback.format_exc())

# Create Flask app
app = Flask(__name__)

def test_environment_variables():
    """Test if all required environment variables are set."""
    logging.info("=== TESTING ENVIRONMENT VARIABLES ===")
    
    required_vars = {
        "WOOT_API_KEY": WOOT_API_KEY,
        "GMAIL_USER": GMAIL_USER,
        "GMAIL_APP_PASSWORD": GMAIL_APP_PASSWORD,
        "EMAIL_RECIPIENT": EMAIL_RECIPIENT,
        "BUCKET_NAME": BUCKET_NAME
    }
    
    all_set = True
    for name, value in required_vars.items():
        if not value:
            logging.error(f"Environment variable {name} is not set")
            all_set = False
        else:
            # Log the first and last few characters of sensitive values
            if name in ["WOOT_API_KEY", "GMAIL_APP_PASSWORD"]:
                masked_value = f"{value[:3]}...{value[-3:]}" if len(value) > 6 else "***"
                logging.info(f"{name} is set: {masked_value}")
            else:
                logging.info(f"{name} is set: {value}")
                
    if all_set:
        logging.info("All required environment variables are set")
    else:
        logging.error("Some required environment variables are missing")
    
    return all_set

def test_storage_access():
    """Test access to Cloud Storage."""
    logging.info("=== TESTING CLOUD STORAGE ACCESS ===")
    
    if not storage_client:
        logging.error("Storage client initialization failed")
        return False
    
    try:
        # Check if the bucket exists
        bucket = storage_client.bucket(BUCKET_NAME)
        exists = bucket.exists()
        
        if exists:
            logging.info(f"Bucket {BUCKET_NAME} exists")
            
            # Test writing to the bucket
            test_blob = bucket.blob("test_access.txt")
            test_blob.upload_from_string(f"Test access at {datetime.now().isoformat()}")
            logging.info("Successfully wrote test file to bucket")
            
            # Test reading from the bucket
            content = test_blob.download_as_text()
            logging.info(f"Successfully read test file from bucket: {content}")
            
            # Clean up
            test_blob.delete()
            logging.info("Successfully deleted test file from bucket")
            
            return True
        else:
            logging.error(f"Bucket {BUCKET_NAME} does not exist")
            return False
    except Exception as e:
        logging.error(f"Error testing storage access: {e}")
        logging.error(traceback.format_exc())
        return False

def test_woot_api():
    """Test connection to Woot API."""
    logging.info("=== TESTING WOOT API CONNECTION ===")
    
    if not WOOT_API_KEY:
        logging.error("WOOT_API_KEY is not set")
        return False
    
    headers = {
        "x-api-key": WOOT_API_KEY,
        "Accept": "application/json"
    }
    
    try:
        # Test the feed endpoint
        logging.info(f"Testing connection to feed endpoint: {FEED_ENDPOINT}")
        response = requests.get(FEED_ENDPOINT, headers=headers)
        
        if response.status_code == 200:
            feed_items = response.json()
            logging.info(f"Successfully connected to feed endpoint. Received {len(feed_items)} items.")
            
            # If we got feed items, test the getoffers endpoint with one item
            if feed_items and len(feed_items) > 0:
                offer_id = feed_items[0].get("OfferId")
                if offer_id:
                    logging.info(f"Testing connection to getoffers endpoint with OfferId: {offer_id}")
                    getoffers_headers = {
                        "x-api-key": WOOT_API_KEY,
                        "Accept": "application/json",
                        "Content-Type": "application/json"
                    }
                    
                    getoffers_response = requests.post(
                        GETOFFERS_ENDPOINT,
                        headers=getoffers_headers,
                        data=json.dumps([offer_id])
                    )
                    
                    if getoffers_response.status_code == 200:
                        detailed_offers = getoffers_response.json()
                        logging.info(f"Successfully connected to getoffers endpoint. Received {len(detailed_offers)} detailed offers.")
                        return True
                    else:
                        logging.error(f"Failed to connect to getoffers endpoint. Status code: {getoffers_response.status_code}")
                        logging.error(f"Response: {getoffers_response.text}")
                        return False
                else:
                    logging.warning("No OfferId found in feed items. Cannot test getoffers endpoint.")
                    return False
            else:
                logging.warning("No feed items received. Cannot test getoffers endpoint.")
                return True  # Still return True as feed endpoint worked
        else:
            logging.error(f"Failed to connect to feed endpoint. Status code: {response.status_code}")
            logging.error(f"Response: {response.text}")
            return False
    except Exception as e:
        logging.error(f"Error testing Woot API: {e}")
        logging.error(traceback.format_exc())
        return False

def test_email():
    """Test email functionality."""
    logging.info("=== TESTING EMAIL FUNCTIONALITY ===")
    
    if not GMAIL_USER or not GMAIL_APP_PASSWORD or not EMAIL_RECIPIENT:
        logging.error("Email configuration is incomplete. Check GMAIL_USER, GMAIL_APP_PASSWORD, and EMAIL_RECIPIENT.")
        return False
    
    try:
        # Create a test email
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f"Test Email from Woot Deals Service ({datetime.now().isoformat()})"
        msg['From'] = GMAIL_USER
        msg['To'] = EMAIL_RECIPIENT
        
        text_content = "This is a test email to verify the email functionality of the Woot Deals service."
        html_content = f"""
        <html>
        <body>
            <h2>Woot Deals Service - Test Email</h2>
            <p>This is a test email to verify that the email functionality is working correctly.</p>
            <p>Timestamp: {datetime.now().isoformat()}</p>
            <hr>
            <p><small>Sent by your Woot Kindle Deals alert system</small></p>
        </body>
        </html>
        """
        
        part1 = MIMEText(text_content, 'plain')
        part2 = MIMEText(html_content, 'html')
        
        msg.attach(part1)
        msg.attach(part2)
        
        # Send the email
        logging.info(f"Attempting to send test email from {GMAIL_USER} to {EMAIL_RECIPIENT}")
        
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            try:
                logging.info("Connecting to SMTP server...")
                server.ehlo()
                logging.info("SMTP server connected")
                
                logging.info("Attempting login...")
                server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
                logging.info("Login successful")
                
                logging.info("Sending email...")
                server.send_message(msg)
                logging.info("Test email sent successfully")
                return True
            except smtplib.SMTPAuthenticationError as e:
                logging.error(f"SMTP Authentication Error: {e}")
                logging.error("This is likely due to incorrect GMAIL_USER or GMAIL_APP_PASSWORD")
                logging.error("Make sure you're using an App Password, not your regular password")
                logging.error("App Passwords must be generated from your Google Account security settings")
                return False
            except Exception as e:
                logging.error(f"SMTP Error: {e}")
                logging.error(traceback.format_exc())
                return False
    except Exception as e:
        logging.error(f"Error testing email functionality: {e}")
        logging.error(traceback.format_exc())
        return False

def load_seen_deals():
    """Load seen deals from Cloud Storage."""
    logging.info("Attempting to load seen deals from Cloud Storage")
    try:
        if not storage_client:
            logging.error("Storage client not initialized")
            return []
            
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(SEEN_DEALS_FILENAME)
        
        if not blob.exists():
            logging.info(f"Seen deals file '{SEEN_DEALS_FILENAME}' does not exist in bucket '{BUCKET_NAME}'. Returning empty list.")
            return []
            
        seen_deals_content = blob.download_as_text()
        seen_deals = json.loads(seen_deals_content)
        logging.info(f"Loaded {len(seen_deals)} seen deals from Cloud Storage")
        return seen_deals
    except Exception as e:
        logging.error(f"Error loading seen deals: {e}")
        logging.error(traceback.format_exc())
        return []

def save_seen_deals(seen_deals):
    """Save seen deals to Cloud Storage."""
    logging.info(f"Attempting to save {len(seen_deals)} seen deals to Cloud Storage")
    try:
        if not storage_client:
            logging.error("Storage client not initialized")
            return False
            
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(SEEN_DEALS_FILENAME)
        blob.upload_from_string(json.dumps(seen_deals))
        logging.info(f"Saved {len(seen_deals)} seen deals to Cloud Storage")
        return True
    except Exception as e:
        logging.error(f"Error saving seen deals: {e}")
        logging.error(traceback.format_exc())
        return False

def fetch_feed():
    """Fetch the feed from the Woot API."""
    logging.info("Fetching feed from Woot API")
    headers = {
        "x-api-key": WOOT_API_KEY,
        "Accept": "application/json"
    }
    try:
        logging.info(f"Making request to {FEED_ENDPOINT}")
        response = requests.get(FEED_ENDPOINT, headers=headers)
        logging.info(f"Received response with status code: {response.status_code}")
        
        if response.status_code != 200:
            logging.error(f"Error response: {response.text}")
            return []
            
        response.raise_for_status()
        feed_items = response.json()
        logging.info(f"Fetched {len(feed_items)} feed items from the API.")
        
        # Log a sample of the feed items
        if feed_items and len(feed_items) > 0:
            sample_item = feed_items[0]
            logging.info(f"Sample feed item: {json.dumps(sample_item, indent=2)}")
            
        return feed_items
    except Exception as e:
        logging.error(f"Error fetching feed: {e}")
        logging.error(traceback.format_exc())
        return []

def fetch_detailed_offers(offer_ids):
    """Fetch detailed information for the specified offer IDs."""
    if not offer_ids:
        logging.info("No offer IDs provided. Skipping detailed offers fetch.")
        return []
    
    # Limit to 25 offers per request, as per API limits
    offer_ids = offer_ids[:25]
    logging.info(f"Fetching detailed information for {len(offer_ids)} offer IDs")
    
    headers = {
        "x-api-key": WOOT_API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    
    try:
        logging.info(f"Making request to {GETOFFERS_ENDPOINT}")
        logging.info(f"Request data: {json.dumps(offer_ids)}")
        
        response = requests.post(
            GETOFFERS_ENDPOINT, 
            headers=headers, 
            data=json.dumps(offer_ids)
        )
        
        logging.info(f"Received response with status code: {response.status_code}")
        
        if response.status_code != 200:
            logging.error(f"Error response: {response.text}")
            return []
            
        response.raise_for_status()
        detailed_offers = response.json()
        logging.info(f"Fetched {len(detailed_offers)} detailed offers from the API.")
        
        # Log a sample of the detailed offers
        if detailed_offers and len(detailed_offers) > 0:
            sample_offer = detailed_offers[0]
            logging.info(f"Sample detailed offer: {json.dumps(sample_offer, indent=2)}")
            
        return detailed_offers
    except Exception as e:
        logging.error(f"Error fetching detailed offers: {e}")
        logging.error(traceback.format_exc())
        return []

def is_matching_deal(deal):
    """Check if a deal matches our keywords."""
    deal_id = deal.get("Id", "unknown")
    logging.info(f"Checking if deal {deal_id} matches keywords")
    
    # Check title
    title = deal.get("Title", "").lower()
    if any(keyword.lower() in title for keyword in KEYWORDS):
        logging.info(f"Deal {deal_id} matches keywords in title: {title}")
        return True
    
    # Check description/writeup
    writeup = deal.get("WriteUpBody", "").lower()
    if any(keyword.lower() in writeup for keyword in KEYWORDS):
        logging.info(f"Deal {deal_id} matches keywords in writeup")
        return True
    
    # Check features
    features = deal.get("Features", "").lower()
    if any(keyword.lower() in features for keyword in KEYWORDS):
        logging.info(f"Deal {deal_id} matches keywords in features")
        return True
        
    logging.info(f"Deal {deal_id} does not match any keywords")
    return False

def filter_deals(deals, seen_deals):
    """Filter deals that match our keywords and haven't been seen before."""
    logging.info(f"Filtering {len(deals)} deals against {len(seen_deals)} seen deals")
    new_matching_deals = []
    
    for deal in deals:
        unique_id = deal.get("Id")
        if not unique_id:
            logging.warning(f"Deal has no Id: {json.dumps(deal, indent=2)}")
            continue
            
        if unique_id in seen_deals:
            logging.info(f"Deal {unique_id} has been seen before, skipping")
            continue
            
        if is_matching_deal(deal):
            logging.info(f"Found new matching deal: {unique_id}")
            new_matching_deals.append(deal)
            
    logging.info(f"Found {len(new_matching_deals)} new matching deals.")
    return new_matching_deals

def format_deal_email(deal):
    """Format a deal for email notification."""
    deal_id = deal.get("Id", "unknown")
    logging.info(f"Formatting email for deal {deal_id}")
    
    title = deal.get("Title", "No Title")
    url = deal.get("Url", "No URL")
    
    # Get price information
    items = deal.get("Items", [])
    if items:
        sale_price = items[0].get("SalePrice", "Unknown")
        list_price = items[0].get("ListPrice", "Unknown")
        savings = ""
        if isinstance(sale_price, (int, float)) and isinstance(list_price, (int, float)):
            if list_price > sale_price:
                savings = f" (Save ${list_price - sale_price:.2f})"
        price_info = f"${sale_price}{savings}"
    else:
        price_info = "Price unknown"
    
    # Extract a snippet from the description
    description = deal.get("WriteUpIntro", "")
    if not description:
        description = deal.get("Snippet", "")
    if len(description) > 200:
        description = description[:197] + "..."
    
    # Format the email
    email_body = f"""
<h2>{title}</h2>
<p><strong>Price:</strong> {price_info}</p>
<p>{description}</p>
<p><a href="{url}">View on Woot!</a></p>
<hr>
<p><small>Sent by your Woot Kindle Deals alert system</small></p>
"""
    logging.info(f"Email formatted for deal {deal_id}")
    return title, email_body

def send_email(deals):
    """Send an email notification for new deals."""
    if not deals:
        logging.info("No deals to send email for. Skipping email notification.")
        return
        
    logging.info(f"Preparing to send email for {len(deals)} deals")
    try:
        # Create message container
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f"Kindle Alert: {len(deals)} new e-reader deal(s) on Woot!"
        msg['From'] = GMAIL_USER
        msg['To'] = EMAIL_RECIPIENT
        
        logging.info(f"Email subject: {msg['Subject']}")
        logging.info(f"Email from: {msg['From']}")
        logging.info(f"Email to: {msg['To']}")
        
        # Create the body of the message
        text_parts = []
        html_parts = []
        
        for deal in deals:
            title, html_content = format_deal_email(deal)
            text_parts.append(f"{title} - {deal.get('Url', 'No URL')}")
            html_parts.append(html_content)
        
        text_content = "\n\n".join(text_parts)
        html_content = "<html><body>" + "".join(html_parts) + "</body></html>"
        
        part1 = MIMEText(text_content, 'plain')
        part2 = MIMEText(html_content, 'html')
        
        msg.attach(part1)
        msg.attach(part2)
        
        logging.info("Email message prepared, attempting to send")
        
        # Send the message via Gmail SMTP
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            logging.info("Connected to SMTP server")
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            logging.info("Logged in to SMTP server")
            server.send_message(msg)
            logging.info("Email sent successfully")
            
        logging.info(f"Sent email notification for {len(deals)} deals.")
        
    except Exception as e:
        logging.error(f"Error sending email: {e}")
        logging.error(traceback.format_exc())

def run_all_tests():
    """Run all diagnostic tests."""
    logging.info("====== RUNNING ALL DIAGNOSTIC TESTS ======")
    
    results = {
        "environment_variables": test_environment_variables(),
        "storage_access": test_storage_access(),
        "woot_api": test_woot_api(),
        "email": test_email()
    }
    
    logging.info("====== TEST RESULTS SUMMARY ======")
    all_passed = True
    for test_name, result in results.items():
        logging.info(f"{test_name}: {'PASS' if result else 'FAIL'}")
        if not result:
            all_passed = False
    
    logging.info(f"Overall test result: {'PASS' if all_passed else 'FAIL'}")
    return results

def check_woot_deals(request):
    """
    Main function to check for Woot deals.
    This function can be triggered by HTTP request or Cloud Scheduler.
    """
    # Extract test mode from request if provided
    test_mode = None
    if request and hasattr(request, 'args') and request.args:
        test_mode = request.args.get('test')
    
    logging.info(f"====== STARTING WOOT DEALS CHECK {'(TEST MODE: ' + test_mode + ')' if test_mode else ''} ======")
    
    # If a specific test is requested, run only that test
    if test_mode:
        if test_mode == "env":
            test_environment_variables()
            return "Environment variables test completed. Check logs for results."
        elif test_mode == "storage":
            test_storage_access()
            return "Storage access test completed. Check logs for results."
        elif test_mode == "api":
            test_woot_api()
            return "Woot API test completed. Check logs for results."
        elif test_mode == "email":
            test_email()
            return "Email test completed. Check logs for results."
        elif test_mode == "all":
            run_all_tests()
            return "All diagnostic tests completed. Check logs for results."
    
    # Regular operation
    logging.info("Starting regular operation")
    
    # Log environment variables status
    env_vars_set = test_environment_variables()
    if not env_vars_set:
        logging.error("Missing required environment variables. Cannot proceed.")
        return "Error: Missing required environment variables"
    
    # Load previously seen deal IDs
    seen_deals = load_seen_deals()
    
    # Step 1: Fetch the feed to get offer IDs
    feed_items = fetch_feed()
    if not feed_items:
        logging.info("No feed items found. Exiting.")
        return "No feed items found"
        
    # Extract offer IDs
    offer_ids = [item.get("OfferId") for item in feed_items if item.get("OfferId")]
    logging.info(f"Extracted {len(offer_ids)} offer IDs from feed items")
    
    # Step 2: Fetch detailed information for these offers
    detailed_offers = fetch_detailed_offers(offer_ids)
    
    # Step 3: Filter for new matching deals
    new_matching_deals = filter_deals(detailed_offers, seen_deals)
    
    # Step 4: Send email notifications if we found any deals
    if new_matching_deals:
        logging.info(f"Found {len(new_matching_deals)} new matching deals. Sending email notification.")
        send_email(new_matching_deals)
        
        # Add to seen deals
        for deal in new_matching_deals:
            seen_deals.append(deal.get("Id"))
            
        # Save updated seen deals
        save_seen_deals(seen_deals)
        result_message = f"Found and notified about {len(new_matching_deals)} new deals"
        logging.info(result_message)
        return result_message
    else:
        result_message = "No new matching deals found."
        logging.info(result_message)
        return result_message

# Add catch-all route handlers
@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def catch_all(path):
    try:
        if path == 'health':
            return "OK", 200
        elif request.args.get('test'):
            test_mode = request.args.get('test')
            return check_woot_deals(request)
        else:
            return check_woot_deals(request)
    except Exception as e:
        logging.error(f"Error handling request: {e}")
        logging.error(traceback.format_exc())
        return f"Error: {str(e)}", 500

# Keep the health endpoint for backward compatibility
@app.route('/health', methods=['GET'])
def health_check():
    return "OK", 200

# Start the server when run directly
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    logging.info(f"Starting Flask server on port {port}")
    app.run(host='0.0.0.0', port=port)