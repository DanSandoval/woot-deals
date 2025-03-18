import requests
import json
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import os
from google.cloud import storage

# Set up logging
logging.basicConfig(level=logging.INFO)

# Configuration (use environment variables for sensitive data)
WOOT_API_KEY = os.environ.get("WOOT_API_KEY")
FEED_ENDPOINT = "https://developer.woot.com/Affiliates/feed/Electronics"
GETOFFERS_ENDPOINT = "https://developer.woot.com/Affiliates/getoffers"
KEYWORDS = ["kindle", "ereader", "e-reader", "e-ink", "kobo", "nook", "eink"]

# Gmail configuration
GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
EMAIL_RECIPIENT = os.environ.get("EMAIL_RECIPIENT")

# GCS configuration
BUCKET_NAME = os.environ.get("BUCKET_NAME")
SEEN_DEALS_FILENAME = "seen_deals.json"

# Initialize storage client
storage_client = storage.Client()


def load_seen_deals():
    """Load seen deals from Cloud Storage."""
    try:
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(SEEN_DEALS_FILENAME)
        
        if not blob.exists():
            return []
            
        seen_deals_content = blob.download_as_text()
        return json.loads(seen_deals_content)
    except Exception as e:
        logging.error(f"Error loading seen deals: {e}")
        return []


def save_seen_deals(seen_deals):
    """Save seen deals to Cloud Storage."""
    try:
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(SEEN_DEALS_FILENAME)
        blob.upload_from_string(json.dumps(seen_deals))
        logging.info(f"Saved {len(seen_deals)} seen deals to Cloud Storage")
    except Exception as e:
        logging.error(f"Error saving seen deals: {e}")


def fetch_feed():
    """Fetch the feed from the Woot API."""
    headers = {
        "x-api-key": WOOT_API_KEY,
        "Accept": "application/json"
    }
    try:
        response = requests.get(FEED_ENDPOINT, headers=headers)
        response.raise_for_status()
        feed_items = response.json()
        logging.info(f"Fetched {len(feed_items)} feed items from the API.")
        return feed_items
    except Exception as e:
        logging.error(f"Error fetching feed: {e}")
        return []


def fetch_detailed_offers(offer_ids):
    """Fetch detailed information for the specified offer IDs."""
    if not offer_ids:
        return []
    
    # Limit to 25 offers per request, as per API limits
    offer_ids = offer_ids[:25]
    
    headers = {
        "x-api-key": WOOT_API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    
    try:
        response = requests.post(
            GETOFFERS_ENDPOINT, 
            headers=headers, 
            data=json.dumps(offer_ids)
        )
        response.raise_for_status()
        detailed_offers = response.json()
        logging.info(f"Fetched {len(detailed_offers)} detailed offers from the API.")
        return detailed_offers
    except Exception as e:
        logging.error(f"Error fetching detailed offers: {e}")
        return []


def is_matching_deal(deal):
    """Check if a deal matches our keywords."""
    # Check title
    title = deal.get("Title", "").lower()
    if any(keyword.lower() in title for keyword in KEYWORDS):
        return True
    
    # Check description/writeup
    writeup = deal.get("WriteUpBody", "").lower()
    if any(keyword.lower() in writeup for keyword in KEYWORDS):
        return True
    
    # Check features
    features = deal.get("Features", "").lower()
    if any(keyword.lower() in features for keyword in KEYWORDS):
        return True
        
    return False


def filter_deals(deals, seen_deals):
    """Filter deals that match our keywords and haven't been seen before."""
    new_matching_deals = []
    
    for deal in deals:
        unique_id = deal.get("Id")
        if not unique_id or unique_id in seen_deals:
            continue
            
        if is_matching_deal(deal):
            new_matching_deals.append(deal)
            
    logging.info(f"Found {len(new_matching_deals)} new matching deals.")
    return new_matching_deals


def format_deal_email(deal):
    """Format a deal for email notification."""
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
    return title, email_body


def send_email(deals):
    """Send an email notification for new deals."""
    if not deals:
        return
        
    try:
        # Create message container
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f"Kindle Alert: {len(deals)} new e-reader deal(s) on Woot!"
        msg['From'] = GMAIL_USER
        msg['To'] = EMAIL_RECIPIENT
        
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
        
        # Send the message via Gmail SMTP
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.send_message(msg)
            
        logging.info(f"Sent email notification for {len(deals)} deals.")
        
    except Exception as e:
        logging.error(f"Error sending email: {e}")


def check_woot_deals(request):
    """
    Cloud Function entry point.
    This function can be triggered by HTTP request or Cloud Scheduler.
    """
    # Load previously seen deal IDs
    seen_deals = load_seen_deals()
    
    # Step 1: Fetch the feed to get offer IDs
    feed_items = fetch_feed()
    if not feed_items:
        logging.info("No feed items found. Exiting.")
        return "No feed items found"
        
    # Extract offer IDs
    offer_ids = [item.get("OfferId") for item in feed_items if item.get("OfferId")]
    
    # Step 2: Fetch detailed information for these offers
    detailed_offers = fetch_detailed_offers(offer_ids)
    
    # Step 3: Filter for new matching deals
    new_matching_deals = filter_deals(detailed_offers, seen_deals)
    
    # Step 4: Send email notifications if we found any deals
    if new_matching_deals:
        send_email(new_matching_deals)
        
        # Add to seen deals
        for deal in new_matching_deals:
            seen_deals.append(deal.get("Id"))
            
        # Save updated seen deals
        save_seen_deals(seen_deals)
        return f"Found and notified about {len(new_matching_deals)} new deals"
    else:
        logging.info("No new matching deals found.")
        return "No new matching deals found"