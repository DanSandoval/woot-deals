import argparse
import requests
import os
import sys

def main():
    parser = argparse.ArgumentParser(description='Test the Woot Deals service')
    parser.add_argument('--url', type=str, required=True, help='URL of the Cloud Run service')
    parser.add_argument('--test', type=str, choices=['env', 'storage', 'api', 'email', 'structure', 'all', 'normal'], 
                        default='all', help='Test mode to run')
    
    args = parser.parse_args()
    
    print(f"Testing the Woot Deals service at {args.url}")
    print(f"Running test mode: {args.test}")
    
    # Build the URL with test parameter
    test_url = f"{args.url}?test={args.test}"
    
    # Make the request
    try:
        print("Sending request...")
        response = requests.get(test_url)
        print(f"Response status code: {response.status_code}")
        print(f"Response body: {response.text}")
        
        if response.status_code == 200:
            print("\nTest completed successfully. Check Cloud Run logs for detailed results.")
            print("To view logs, go to Google Cloud Console > Cloud Run > woot-deals > Logs")
        else:
            print("\nTest failed. Check Cloud Run logs for error details.")
    except Exception as e:
        print(f"Error making request: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main() 