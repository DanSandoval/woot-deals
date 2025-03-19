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
FEED_ENDPOINT = "https://developer.woot.com/feed/All"  # Changed to All to search everything
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
            api_response = response.json()
            logging.info(f"Successfully connected to feed endpoint. Received response data.")
            
            # Log details about the response structure
            logging.info(f"Response type: {type(api_response)}")
            
            if isinstance(api_response, dict):
                logging.info(f"API returned dictionary with keys: {list(api_response.keys())}")
                # Log the first level of the response to understand the structure
                for key, value in api_response.items():
                    value_type = type(value)
                    if isinstance(value, (list, dict)):
                        size_info = f" with {len(value)} items" if hasattr(value, "__len__") else ""
                        logging.info(f"Key '{key}' has value of type {value_type}{size_info}")
                    else:
                        value_preview = str(value)[:50] + "..." if len(str(value)) > 50 else str(value)
                        logging.info(f"Key '{key}' has value: {value_preview}")
                
                # Try to find where the item list might be
                potential_items = []
                for key, value in api_response.items():
                    if isinstance(value, list) and len(value) > 0:
                        if isinstance(value[0], dict):
                            logging.info(f"Found potential items list under key '{key}'")
                            logging.info(f"First item keys: {list(value[0].keys())}")
                            potential_items.append((key, value))
                
                # Try to find an offer ID from any potential item lists
                offer_id = None
                for key, items in potential_items:
                    for item in items:
                        if isinstance(item, dict):
                            # Check common ID field names
                            if "OfferId" in item:
                                offer_id = item["OfferId"]
                                logging.info(f"Found OfferId '{offer_id}' in item from '{key}' list")
                                break
                            elif "Id" in item:
                                offer_id = item["Id"]
                                logging.info(f"Found Id '{offer_id}' in item from '{key}' list")
                                break
                    if offer_id:
                        break
            
            elif isinstance(api_response, list):
                logging.info(f"Response is a list with {len(api_response)} items")
                if api_response:
                    sample_item = api_response[0]
                    logging.info(f"Sample item type: {type(sample_item)}")
                    if isinstance(sample_item, dict):
                        logging.info(f"Sample item keys: {list(sample_item.keys())}")
                        # Print a snippet of the sample item
                        logged_sample = {k: v for k, v in sample_item.items() if k in ['OfferId', 'Id', 'Title', 'Url']}
                        logging.info(f"Sample item values: {json.dumps(logged_sample, indent=2)}")
                
                # Try to find an offer ID from the list items
                offer_id = None
                for item in api_response:
                    if isinstance(item, dict):
                        # Check common ID field names
                        if "OfferId" in item:
                            offer_id = item["OfferId"]
                            logging.info(f"Found OfferId: {offer_id}")
                            break
                        elif "Id" in item:
                            offer_id = item["Id"]
                            logging.info(f"Found Id: {offer_id}")
                            break
            
            else:
                logging.info(f"Response is neither a list nor a dict. Type: {type(api_response)}")
                # Dump as string to get a sense of what it is
                logging.info(f"Response preview: {str(api_response)[:500]}...")
                
            # Log full response structure (truncated) for analysis
            response_str = json.dumps(api_response, indent=2)
            logging.info(f"Full response structure (truncated): {response_str[:1000]}...")
                
            # Test the getoffers endpoint if we found an ID
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
                    logging.info(f"Successfully connected to getoffers endpoint. Received {len(detailed_offers) if isinstance(detailed_offers, list) else 'non-list'} response.")
                    
                    if isinstance(detailed_offers, list) and detailed_offers:
                        sample_offer = detailed_offers[0]
                        if isinstance(sample_offer, dict):
                            # Log the structure of a detailed offer
                            logging.info(f"Detailed offer keys: {list(sample_offer.keys())}")
                            logged_offer = {k: v for k, v in sample_offer.items() if k in ['Id', 'Title', 'Url']}
                            logging.info(f"Sample detailed offer: {json.dumps(logged_offer, indent=2)}")
                    
                    return True
                else:
                    logging.error(f"Failed to connect to getoffers endpoint. Status code: {getoffers_response.status_code}")
                    logging.error(f"Response: {getoffers_response.text}")
                    # Still return True as the feed endpoint worked
                    return True
            else:
                logging.warning("No suitable ID found in feed items to test getoffers endpoint. Feed endpoint is working though.")
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
        api_response = response.json()
        
        # Convert to normalized format for processing
        normalized_items = []
        
        if isinstance(api_response, dict):
            # Log the structure of the dictionary to understand it
            logging.info(f"API returned dictionary with keys: {list(api_response.keys())}")
            
            # Try to find where the item list might be
            potential_item_keys = []
            for key, value in api_response.items():
                logging.info(f"Key '{key}' has value of type: {type(value)}")
                if isinstance(value, list):
                    potential_item_keys.append(key)
                    logging.info(f"Found list under key '{key}' with {len(value)} items")
                    
            # Try common field names where items might be
            item_list = None
            for key in ['items', 'data', 'offers', 'results', 'deals', 'feed'] + potential_item_keys:
                if key in api_response and isinstance(api_response[key], list):
                    item_list = api_response[key]
                    logging.info(f"Found items list under key '{key}' with {len(item_list)} items")
                    break
            
            # If we found a list of items, process them
            if item_list:
                for item in item_list:
                    if isinstance(item, dict):
                        # Make sure we have a consistent ID field
                        processed_item = item.copy()
                        
                        # Use OfferId as the primary ID, falling back to Id if needed
                        if "OfferId" in item:
                            processed_item["Id"] = item["OfferId"]
                        elif "Id" in item:
                            processed_item["OfferId"] = item["Id"]
                            
                        normalized_items.append(processed_item)
                        
                # Log full structure of a sample item to understand what's available
                if len(item_list) > 0:
                    logging.info(f"Sample raw item: {json.dumps(item_list[0], indent=2)[:500]}...")
            else:
                # If we couldn't find a list, try to extract fields from the response itself
                logging.info(f"Could not find a list of items in the response structure")
                logging.info(f"Full API response structure: {json.dumps(api_response, indent=2)[:500]}...")
                
                # As a fallback, if the response is short, add it as a single item
                normalized_items.append({
                    "Id": "response",
                    "OfferId": "response",
                    "RawResponse": api_response
                })
                
        elif isinstance(api_response, list):
            logging.info(f"API returned list with {len(api_response)} items")
            
            for item in api_response:
                if isinstance(item, dict):
                    # Make sure we have a consistent ID field
                    processed_item = item.copy()
                    
                    # Use OfferId as the primary ID, falling back to Id if needed
                    if "OfferId" in item:
                        processed_item["Id"] = item["OfferId"]
                    elif "Id" in item:
                        processed_item["OfferId"] = item["Id"]
                        
                    normalized_items.append(processed_item)
            
            # Log full structure of a sample item to understand what's available
            if len(api_response) > 0:
                logging.info(f"Sample raw item: {json.dumps(api_response[0], indent=2)[:500]}...")
        else:
            logging.warning(f"API returned unexpected response type: {type(api_response)}")
                    
        # Log a sample of the normalized items
        if normalized_items and len(normalized_items) > 0:
            sample_item = normalized_items[0]
            logging.info(f"Sample normalized item: {json.dumps({k: v for k, v in sample_item.items() if k in ['Id', 'OfferId', 'Title', 'Url']}, indent=2)}")
            
        logging.info(f"Normalized {len(normalized_items)} items from the API response")
        return normalized_items
    except Exception as e:
        logging.error(f"Error fetching feed: {e}")
        logging.error(traceback.format_exc())
        return []

def fetch_detailed_offers(offer_ids):
    """Fetch detailed information for the specified offer IDs."""
    if not offer_ids:
        logging.info("No offer IDs provided. Skipping detailed offers fetch.")
        return []
    
    # Process the batch of offer IDs (25 at a time is the API limit)
    batch_size = 25
    batch_ids = offer_ids[:batch_size]
    logging.info(f"Fetching detailed information for {len(batch_ids)} offer IDs")
    
    headers = {
        "x-api-key": WOOT_API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    
    try:
        logging.info(f"Making request to {GETOFFERS_ENDPOINT}")
        logging.info(f"Request data: {json.dumps(batch_ids)}")
        
        response = requests.post(
            GETOFFERS_ENDPOINT, 
            headers=headers, 
            data=json.dumps(batch_ids)
        )
        
        logging.info(f"Received response with status code: {response.status_code}")
        
        if response.status_code != 200:
            logging.error(f"Error response: {response.text}")
            return []
            
        response.raise_for_status()
        detailed_offers = response.json()
        
        if isinstance(detailed_offers, list):
            logging.info(f"Fetched {len(detailed_offers)} detailed offers from the API.")
            
            # Log a sample of the detailed offers
            if detailed_offers and len(detailed_offers) > 0:
                sample_offer = detailed_offers[0]
                logging.info(f"Sample detailed offer structure: {json.dumps({k: v for k, v in sample_offer.items() if k in ['Id', 'Title', 'Url']}, indent=2)}")
                
            return detailed_offers
        else:
            logging.warning(f"Detailed offers response is not a list: {type(detailed_offers)}")
            return []
    except Exception as e:
        logging.error(f"Error fetching detailed offers: {e}")
        logging.error(traceback.format_exc())
        return []

def is_matching_deal(deal):
    """Check if a deal matches our keywords."""
    # Use either Id or OfferId, whichever is available
    deal_id = deal.get("Id", deal.get("OfferId", "unknown"))
    logging.info(f"Checking if deal {deal_id} matches keywords")
    
    # Check title
    title = deal.get("Title", "") or ""
    if any(keyword.lower() in title.lower() for keyword in KEYWORDS):
        logging.info(f"Deal {deal_id} matches keywords in title: {title}")
        return True
    
    # Check description/writeup
    writeup = deal.get("WriteUpBody", "") or ""
    if any(keyword.lower() in writeup.lower() for keyword in KEYWORDS):
        logging.info(f"Deal {deal_id} matches keywords in writeup")
        return True
    
    # Check features
    features = deal.get("Features", "") or ""
    if any(keyword.lower() in features.lower() for keyword in KEYWORDS):
        logging.info(f"Deal {deal_id} matches keywords in features")
        return True
        
    # Check subtitle if available
    subtitle = deal.get("Subtitle", "") or ""
    if any(keyword.lower() in subtitle.lower() for keyword in KEYWORDS):
        logging.info(f"Deal {deal_id} matches keywords in subtitle")
        return True
    
    # Check snippet if available
    snippet = deal.get("Snippet", "") or ""
    if any(keyword.lower() in snippet.lower() for keyword in KEYWORDS):
        logging.info(f"Deal {deal_id} matches keywords in snippet")
        return True
        
    logging.info(f"Deal {deal_id} does not match any keywords")
    return False

def filter_deals(deals, seen_deals):
    """Filter deals that match our keywords and haven't been seen before."""
    logging.info(f"Filtering {len(deals)} deals against {len(seen_deals)} seen deals")
    new_matching_deals = []
    
    for deal in deals:
        # Try both Id and OfferId fields for compatibility
        unique_id = deal.get("Id", deal.get("OfferId"))
        if not unique_id:
            logging.warning(f"Deal has no Id or OfferId: {json.dumps({k: v for k, v in deal.items() if k in ['Title', 'Url']}, indent=2)}")
            continue
            
        if unique_id in seen_deals:
            logging.info(f"Deal {unique_id} has been seen before, skipping")
            continue
            
        if is_matching_deal(deal):
            logging.info(f"Found new matching deal: {unique_id}")
            new_matching_deals.append(deal)
            
    logging.info(f"Found {len(new_matching_deals)} new matching deals.")
    return new_matching_deals

def format_deal_notifications(deal):
    """Format a deal for both email and text notifications."""
    deal_id = deal.get("Id", deal.get("OfferId", "unknown"))
    logging.info(f"Formatting notifications for deal {deal_id}")
    
    title = deal.get("Title", "No Title")
    url = deal.get("Url", "No URL")
    
    # Get price information - handle different possible structures
    sale_price = None
    list_price = None
    price_info = "Price unknown"
    
    # Try to get price from Items field
    items = deal.get("Items", [])
    if items and isinstance(items, list) and len(items) > 0:
        sale_price = items[0].get("SalePrice", None)
        list_price = items[0].get("ListPrice", None)
        
        if sale_price is not None:
            price_info = f"${sale_price}"
    
    # Try SalePrice field directly on the deal
    elif "SalePrice" in deal:
        sale_price_data = deal.get("SalePrice")
        list_price = deal.get("ListPrice")
        
        if isinstance(sale_price_data, list) and len(sale_price_data) > 0:
            # Handle price range format
            min_price = sale_price_data[0].get("Minimum", None)
            if min_price is not None:
                sale_price = min_price
                price_info = f"${min_price}"
        elif sale_price_data is not None:
            sale_price = sale_price_data
            price_info = f"${sale_price}"
    
    # Format savings info if we have both prices
    savings_info = ""
    if sale_price is not None and list_price is not None:
        if isinstance(sale_price, (int, float)) and isinstance(list_price, (int, float)):
            if list_price > sale_price:
                savings_info = f" (Save ${list_price - sale_price:.2f})"
    
    # 1. Create short text message (<140 chars)
    list_price_text = f" (Was ${list_price})" if list_price is not None else ""
    text_message = f"{title[:70]}... {price_info}{list_price_text}"
    
    # Ensure we're under 140 chars
    if len(text_message) > 140:
        # Truncate title further if needed
        max_title_len = 70 - (len(text_message) - 140)
        if max_title_len < 10:
            max_title_len = 10
        text_message = f"{title[:max_title_len]}... {price_info}{list_price_text}"
    
    # 2. Format the detailed email
    email_body = f"""
<h2>{title}</h2>
<p><strong>Price:</strong> {price_info}{savings_info}</p>
<p><strong>URL:</strong> <a href="{url}">{url}</a></p>
<hr>
<p><small>Sent by your Woot Deals alert system</small></p>
"""
    
    logging.info(f"Notifications formatted for deal {deal_id}")
    return title, email_body, text_message

def send_notifications(deals):
    """Send email and text notifications for new deals."""
    if not deals:
        logging.info("No deals to send notifications for. Skipping.")
        return
        
    logging.info(f"Preparing to send notifications for {len(deals)} deals")
    try:
        # Send text messages to the phone number
        text_msg = MIMEMultipart('alternative')
        text_msg['Subject'] = f"Woot Deal Alert"
        text_msg['From'] = GMAIL_USER
        text_msg['To'] = EMAIL_RECIPIENT  # This is the phone number email
        
        # Send detailed emails to the sender's address
        email_msg = MIMEMultipart('alternative')
        email_msg['Subject'] = f"Woot Alert: {len(deals)} new deal(s) matching your keywords"
        email_msg['From'] = GMAIL_USER
        email_msg['To'] = GMAIL_USER  # Send to yourself
        
        text_parts = []
        html_parts = []
        sms_parts = []
        
        for deal in deals:
            title, html_content, sms_content = format_deal_notifications(deal)
            text_parts.append(f"{title} - {deal.get('Url', 'No URL')}")
            html_parts.append(html_content)
            sms_parts.append(sms_content)
        
        # For SMS - use the short format
        sms_content = "\n".join(sms_parts)
        text_msg.attach(MIMEText(sms_content, 'plain'))
        
        # For email - use full HTML
        text_content = "\n\n".join(text_parts)
        html_content = "<html><body>" + "".join(html_parts) + "</body></html>"
        email_msg.attach(MIMEText(text_content, 'plain'))
        email_msg.attach(MIMEText(html_content, 'html'))
        
        # Send both messages
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            
            # Send text message first
            server.send_message(text_msg)
            logging.info("Text message sent successfully")
            
            # Send detailed email
            server.send_message(email_msg)
            logging.info("Email notification sent successfully")
            
        logging.info(f"Sent notifications for {len(deals)} deals")
        
    except Exception as e:
        logging.error(f"Error sending notifications: {e}")
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

def title_contains_keywords(title):
    """
    Check if the title contains any of our keywords.
    This is used for pre-filtering to reduce API calls.
    """
    if not title:
        return False
    
    title_lower = title.lower()
    return any(keyword.lower() in title_lower for keyword in KEYWORDS)

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
    
    # Step 1: Fetch the feed to get basic deal information
    feed_items = fetch_feed()
    if not feed_items:
        logging.info("No feed items found. Exiting.")
        return "No feed items found"
    
    # Step 2: Pre-filter feed items by title to reduce API calls
    potential_matches = []
    all_offer_ids = []  # Track all offers for seen deals list
    
    for item in feed_items:
        # Get the ID for tracking
        offer_id = None
        if "OfferId" in item:
            offer_id = item["OfferId"]
        elif "Id" in item:
            offer_id = item["Id"]
        
        if not offer_id:
            continue
            
        # Add to all offers list
        all_offer_ids.append(offer_id)
        
        # Skip if already seen
        if offer_id in seen_deals:
            logging.info(f"Deal {offer_id} has been seen before, skipping")
            continue
        
        # Check if title contains any keywords
        title = item.get("Title", "")
        if title_contains_keywords(title):
            logging.info(f"Pre-filter match: '{title}' contains keywords")
            potential_matches.append(offer_id)
    
    logging.info(f"Pre-filtered {len(feed_items)} items down to {len(potential_matches)} potential matches")
    
    # If no potential matches from title screening, we're done
    if not potential_matches:
        # Add all offer IDs to seen deals to avoid checking them again
        seen_deals.extend(all_offer_ids)
        save_seen_deals(seen_deals)
        
        logging.info("No potential matches found in pre-filtering. Exiting.")
        return "No matching deals found."
    
    # Step 3: Process potential matches in batches of 25 (API limit)
    all_matching_deals = []
    batch_size = 25
    total_batches = (len(potential_matches) + batch_size - 1) // batch_size  # Ceiling division
    
    logging.info(f"Processing {len(potential_matches)} potential matches in {total_batches} batches of {batch_size}")
    
    for i in range(0, len(potential_matches), batch_size):
        batch_num = i // batch_size + 1
        batch = potential_matches[i:i+batch_size]
        logging.info(f"Processing batch {batch_num}/{total_batches} with {len(batch)} offers")
        
        # Fetch detailed information for this batch
        detailed_offers = fetch_detailed_offers(batch)
        
        # Filter for new matching deals (full check with all fields)
        batch_matching_deals = filter_deals(detailed_offers, seen_deals)
        
        # Add new matches to our results list
        all_matching_deals.extend(batch_matching_deals)
        
        # Add processed IDs to seen deals
        for deal in detailed_offers:
            unique_id = deal.get("Id", deal.get("OfferId"))
            if unique_id and unique_id not in seen_deals:
                seen_deals.append(unique_id)
    
    # Step 4: Send notifications if we found any matching deals
    if all_matching_deals:
        logging.info(f"Found a total of {len(all_matching_deals)} new matching deals. Sending notifications.")
        send_notifications(all_matching_deals)
        
        # Save updated seen deals (includes all IDs we've processed)
        save_seen_deals(seen_deals)
        result_message = f"Found and notified about {len(all_matching_deals)} new deals"
        logging.info(result_message)
        return result_message
    else:
        # Add all offer IDs to seen deals to avoid checking them again
        seen_deals.extend([id for id in all_offer_ids if id not in seen_deals])
        save_seen_deals(seen_deals)
        
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