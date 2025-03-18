#!/usr/bin/env python3
import requests
import json
import argparse
import sys

def test_api_endpoints(api_key):
    """Test the Woot API endpoints to ensure they're working correctly."""
    print("Testing Woot API endpoints with your API key...")
    
    # Test feed endpoint
    feed_endpoint = "https://developer.woot.com/feed/Electronics"
    print(f"\nTesting feed endpoint: {feed_endpoint}")
    
    headers = {
        "x-api-key": api_key,
        "Accept": "application/json"
    }
    
    try:
        response = requests.get(feed_endpoint, headers=headers)
        print(f"Status code: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            print(f"Success! Received {len(data)} items from feed")
            
            # Extract first offer ID for testing getoffers endpoint
            if data and len(data) > 0:
                offer_id = data[0].get("OfferId")
                if offer_id:
                    # Test getoffers endpoint
                    getoffers_endpoint = "https://developer.woot.com/getoffers"
                    print(f"\nTesting getoffers endpoint: {getoffers_endpoint}")
                    print(f"Using offer ID: {offer_id}")
                    
                    getoffers_headers = {
                        "x-api-key": api_key,
                        "Accept": "application/json",
                        "Content-Type": "application/json"
                    }
                    
                    getoffers_response = requests.post(
                        getoffers_endpoint,
                        headers=getoffers_headers,
                        data=json.dumps([offer_id])
                    )
                    
                    print(f"Status code: {getoffers_response.status_code}")
                    
                    if getoffers_response.status_code == 200:
                        offers_data = getoffers_response.json()
                        print(f"Success! Received {len(offers_data)} detailed offer(s)")
                        print("\nBoth endpoints are working correctly!")
                        return True
                    else:
                        print(f"Error with getoffers endpoint: {getoffers_response.text}")
                else:
                    print("No offer ID found in the feed data")
            else:
                print("No items found in the feed")
        else:
            print(f"Error with feed endpoint: {response.text}")
    except Exception as e:
        print(f"Error testing API endpoints: {e}")
    
    print("\nEndpoint tests failed. Please check your API key and endpoints.")
    return False

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Test Woot API endpoints')
    parser.add_argument('--api-key', type=str, required=True, help='Your Woot API key')
    
    args = parser.parse_args()
    
    if test_api_endpoints(args.api_key):
        sys.exit(0)
    else:
        sys.exit(1)